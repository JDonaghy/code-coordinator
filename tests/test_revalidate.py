"""#1769: `coord merge --revalidate` — the merge lane's stale-verdict arm.

#1738 gave `coord drive` a re-test arm for a STALE-but-`passed` smoke verdict.
It covered 1 of the 3 real stalls measured on 2026-08-03, because the other two
branches were parked in the merge queue with no live drive. These tests cover
the second lane: the resolution reachable from `coord merge` itself.

Three things are asserted here, in order of how much they matter:

1. **`--revalidate` off ⇒ nothing changes.** Plain `coord merge` must be
   byte-identical to before: no re-test, no verdict write, no merge.
2. **A failing re-test never merges.** This must never become a laundering
   path for a verdict that would not pass against the current base.
3. **Only the stale case is eligible.** Review / CI / conflict / genuinely-
   missing-verdict blocks are untouched, even under `--revalidate`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import merge_queue as mq
from coord import revalidate as rv
from coord.cli import main
from coord.models import Assignment, Board


# ── shared fixtures ──────────────────────────────────────────────────────────

CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
    test_command: "true"
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: {repo_path}
reviews:
  enabled: false
pipeline:
  default_gates: [test, merge]
ci_store:
  type: none
"""


@pytest.fixture(autouse=True)
def isolated_coord_dir(tmp_path: Path, monkeypatch):
    """`revalidate` builds its throwaway worktree under ``COORD_DIR`` — pin
    that to the test's tmp dir so a test run never writes into the real
    ``~/.coord/``."""
    d = tmp_path / "coord-state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("coord.state.COORD_DIR", d)
    return d


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML.format(repo_path=str(tmp_path / "checkout")))
    return p


def _stamp_anchors(aid: str, *, head_sha: str, base_sha: str, patch_id: str) -> None:
    """Persist the #1479 freshness anchors on a board row.

    ``save_board`` does not carry them (they're written by
    ``coord.state._stamp_test_staleness_anchor``, three live `gh` reads that
    ride along with a real verdict write), so a seeded board has them NULL —
    which makes every staleness check a no-op and the entry look fresh.
    """
    from coord.db import get_connection

    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET test_head_sha=?, test_base_sha=?, test_patch_id=? "
        "WHERE assignment_id=?",
        (head_sha, base_sha, patch_id, aid),
    )
    conn.commit()


def _entry(
    aid: str,
    *,
    issue: int,
    state: str = mq.PENDING,
    target: str = "main",
) -> mq.QueuedMerge:
    return mq.QueuedMerge(
        assignment_id=aid,
        repo_name="api",
        repo_github="acme/api",
        branch=f"issue-{issue}-{aid}",
        target_branch=target,
        issue_number=issue,
        issue_title=f"issue {issue}",
        state=state,
    )


def _tested_work(
    aid: str,
    *,
    issue: int,
    test_state: str | None = "passed",
    base_sha: str = "base-old",
) -> Assignment:
    """A done work row carrying a terminal verdict anchored (per #1479) to the
    branch/base it was actually tested against."""
    return Assignment(
        machine_name="laptop", repo_name="api", issue_number=issue,
        issue_title=f"issue {issue}", assignment_id=aid, type="work",
        status="done", branch=f"issue-{issue}-{aid}",
        test_state=test_state,
        test_head_sha=f"branch-sha-{issue}",
        test_base_sha=base_sha,
        test_patch_id=f"patch-{issue}",
    )


def _config(*, gates: list[str] | None = None, reviews: bool = False):
    from dataclasses import dataclass as _dc, field as _f

    @_dc
    class _Reviews:
        enabled: bool = False

    @_dc
    class _Pipeline:
        default_gates: list[str] | None = None

    @_dc
    class _Cfg:
        reviews: _Reviews = _f(default_factory=_Reviews)
        pipeline: _Pipeline = _f(default_factory=_Pipeline)

    cfg = _Cfg()
    cfg.reviews.enabled = reviews
    cfg.pipeline.default_gates = (
        gates if gates is not None else ["test", "review", "merge"]
    )
    return cfg


# ══════════════════════════════════════════════════════════════════════════════
# 1. Stale-vs-missing has exactly ONE implementation
# ══════════════════════════════════════════════════════════════════════════════

class TestSingleStaleDetector:
    """#1769 acceptance: "Stale-vs-missing detection has exactly one
    implementation, imported by both `coord/drive.py` and the merge path —
    assert this with a test that would fail if the logic were duplicated."

    #1738 put the predicate in `coord/drive.py`. #1769 moved it to
    `coord/merge_queue.py`, next to the `SmokeVerdictStatus` code that emits
    both of the wordings it matches, and made drive an alias. A future copy —
    the #1141 failure mode — breaks these.
    """

    def test_drive_and_merge_queue_share_the_same_function_object(self) -> None:
        from coord import drive as drive_mod

        assert drive_mod._is_stale_smoke_reason is mq.is_stale_smoke_reason, (
            "coord.drive must ALIAS coord.merge_queue.is_stale_smoke_reason, "
            "not define its own copy — a second string-matching implementation "
            "in a second module is how #1141 went stale"
        )
        assert drive_mod._STALE_SMOKE_MARKERS is mq.STALE_SMOKE_MARKERS

    def test_revalidate_path_uses_the_same_module(self) -> None:
        """The merge lane consumes the SAME module-level detector, via
        `merge_queue.revalidation_candidates` (structured) — not a third copy
        of the string matching."""
        import inspect

        src = inspect.getsource(rv)
        assert "smoke test verdict is stale" not in src, (
            "coord.revalidate must not carry its own copy of the stale-verdict "
            "marker strings — it consumes merge_queue's classification"
        )
        assert "test verdict stale" not in src

    def test_no_module_outside_merge_queue_defines_the_markers(self) -> None:
        """The literal marker tuple is defined once, in merge_queue.py."""
        coord_dir = Path(mq.__file__).parent
        definers = []
        for path in sorted(coord_dir.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            # A *definition* looks like `... = ("smoke test verdict is stale"`
            for line in text.splitlines():
                stripped = line.strip()
                if (
                    "= (" in stripped
                    and "smoke test verdict is stale" in stripped
                ):
                    definers.append(str(path.relative_to(coord_dir.parent)))
        assert sorted(set(definers)) == ["coord/merge_queue.py"], definers

    def test_distinguishes_stale_from_missing(self) -> None:
        """The behaviour itself, unchanged from #1738's coverage in
        tests/test_drive.py — asserted here too so the lifted home is
        independently pinned."""
        assert mq.is_stale_smoke_reason(
            "smoke test verdict is stale: recorded against base abc1234, "
            "base is now def5678 — re-verify"
        )
        assert mq.is_stale_smoke_reason("test verdict stale (base moved)")
        assert not mq.is_stale_smoke_reason(
            "smoke test required but no verdict recorded"
        )
        assert not mq.is_stale_smoke_reason("test verdict missing")
        assert not mq.is_stale_smoke_reason("review required but not approved")
        assert not mq.is_stale_smoke_reason("")
        assert not mq.is_stale_smoke_reason(None)


# ══════════════════════════════════════════════════════════════════════════════
# 2. Eligibility — only the stale case, only when nothing else blocks
# ══════════════════════════════════════════════════════════════════════════════

class TestRevalidationCandidates:
    @staticmethod
    def _stale_setup():
        """One PENDING entry whose only problem is a base that moved."""
        entry = _entry("w1", issue=101)
        entry.target_branch_head_sha = "base-new"
        board = Board(active=[], completed=[_tested_work("w1", issue=101)])
        return entry, board

    def test_stale_verdict_is_a_candidate(self) -> None:
        entry, board = self._stale_setup()
        cands = mq.revalidation_candidates([entry], board, _config(gates=["test", "merge"]))
        assert [c.entry.assignment_id for c in cands] == ["w1"]
        assert cands[0].work_assignment_id == "w1"
        assert cands[0].smoke.kind == mq.SMOKE_STALE

    def test_missing_verdict_is_not_a_candidate(self) -> None:
        """#1769 acceptance: a genuinely-missing verdict is never revalidated
        — it's the #1640 lost-write shape, which a re-test cannot safely paper
        over."""
        entry = _entry("w1", issue=101)
        board = Board(active=[], completed=[
            _tested_work("w1", issue=101, test_state=None),
        ])
        assert mq.revalidation_candidates(
            [entry], board, _config(gates=["test", "merge"])
        ) == []

    def test_fresh_verdict_is_not_a_candidate(self) -> None:
        entry = _entry("w1", issue=101)
        entry.target_branch_head_sha = "base-old"  # unmoved
        board = Board(active=[], completed=[_tested_work("w1", issue=101)])
        assert mq.revalidation_candidates(
            [entry], board, _config(gates=["test", "merge"])
        ) == []

    def test_review_block_is_not_a_candidate(self) -> None:
        """#1769 acceptance: an entry blocked on review is untouched even under
        --revalidate — a re-test gives it nothing it's waiting for."""
        entry, board = self._stale_setup()
        cfg = _config(gates=["test", "review", "merge"], reviews=True)
        assert mq.revalidation_candidates([entry], board, cfg) == []

    def test_conflict_entry_is_not_a_candidate(self) -> None:
        entry, board = self._stale_setup()
        entry.state = mq.CONFLICT
        assert mq.revalidation_candidates(
            [entry], board, _config(gates=["test", "merge"])
        ) == []

    def test_human_required_entry_is_not_a_candidate(self) -> None:
        entry, board = self._stale_setup()
        entry.state = mq.HUMAN_REQUIRED
        assert mq.revalidation_candidates(
            [entry], board, _config(gates=["test", "merge"])
        ) == []

    def test_smoke_gate_disabled_yields_no_candidates(self) -> None:
        entry, board = self._stale_setup()
        assert mq.revalidation_candidates(
            [entry], board, _config(gates=["merge"])
        ) == []

    def test_is_pure_no_mutation(self) -> None:
        """Safe to call from --dry-run: nothing is written."""
        entry, board = self._stale_setup()
        before = (entry.state, entry.error)
        mq.revalidation_candidates([entry], board, _config(gates=["test", "merge"]))
        assert (entry.state, entry.error) == before


# ══════════════════════════════════════════════════════════════════════════════
# 3. The composite re-test itself
# ══════════════════════════════════════════════════════════════════════════════

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
    )


@pytest.fixture
def git_fleet(tmp_path: Path):
    """A bare 'origin' with `main` plus three feature branches that compose
    cleanly, and a local checkout wired to it — the real shape `revalidate()`
    operates on (throwaway worktree off the base checkout, `origin/<branch>`
    refs).

    Three branches, not two: #1715's headline acceptance is stated over a
    THREE-entry queue ("assert the run count is 1, not 3"). Tests that only
    need two simply queue two — an unused branch in the fleet costs nothing.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"

    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "base.txt").write_text("base\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "base")

    for issue, aid in ((101, "w1"), (102, "w2"), (103, "w3")):
        _git(seed, "checkout", "-q", "-b", f"issue-{issue}-{aid}", "main")
        (seed / f"f{issue}.txt").write_text(f"issue {issue}\n")
        _git(seed, "add", ".")
        _git(seed, "commit", "-q", "-m", f"issue {issue}")
    _git(seed, "checkout", "-q", "main")

    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(origin)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(checkout)],
        check=True, capture_output=True, text=True,
    )
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "t")
    return checkout


@dataclass
class _Run:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _live_config(checkout: Path, *, test_command: str = "true"):
    """A config object shaped like the real one, pointing at *checkout*."""
    @dataclass
    class _Repo:
        name: str = "api"
        github: str = "acme/api"
        default_branch: str = "main"
        test_command: str | None = None
        build_command: str | None = None

    @dataclass
    class _Machine:
        name: str = "laptop"
        host: str = "laptop.tailnet"
        path: str = ""

        def repo_path(self, repo_name: str):
            return self.path if repo_name == "api" else None

    class _Cfg:
        def __init__(self) -> None:
            self._repo = _Repo(test_command=test_command)
            self.machines = [_Machine(path=str(checkout))]

        def repo(self, name):
            return self._repo if name == "api" else None

    return _Cfg()


class TestCompositeRevalidation:
    @staticmethod
    def _candidates():
        out = []
        for aid, issue in (("w1", 101), ("w2", 102)):
            entry = _entry(aid, issue=issue)
            entry.target_branch_head_sha = "base-new"
            out.append(mq.RevalidationCandidate(
                entry=entry,
                work_assignment_id=aid,
                smoke=mq.SmokeVerdictStatus(
                    ok=False, kind=mq.SMOKE_STALE, assignment_id=aid,
                    anchor="base", recorded_sha="base-old", current_sha="base-new",
                ),
            ))
        return out

    def test_batch_composes_all_branches_and_runs_the_suite_once(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """#1715 option 3: N entries → 1 suite run, on a composite of all N."""
        runs: list[tuple[str, Path]] = []

        def runner(command, cwd, timeout):
            runs.append((command, cwd))
            # Every branch's file must be present in the composite tree.
            assert (Path(cwd) / "f101.txt").exists()
            assert (Path(cwd) / "f102.txt").exists()
            return _Run(0)

        recorded: list[tuple] = []
        with patch(
            "coord.state.record_test_verdict",
            side_effect=lambda **kw: recorded.append(kw),
        ):
            result = rv.revalidate(
                self._candidates(), _live_config(git_fleet), runner=runner,
            )

        assert result.ok, result.reason
        assert len(runs) == 1, "the whole point is ONE suite run for N entries"
        assert result.composed == ["issue-101-w1", "issue-102-w2"]
        assert sorted(result.recorded) == ["w1", "w2"]
        assert [r["assignment_id"] for r in recorded] == ["w1", "w2"]
        assert {r["test_state"] for r in recorded} == {"passed"}

    def test_failing_suite_records_nothing_and_reports_the_failure(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """#1769 acceptance: "A revalidation whose re-test FAILS leaves the
        entry blocked and does not merge. ... it must never become a laundering
        path for a verdict that would not pass against the current base."
        """
        def runner(command, cwd, timeout):
            return _Run(1, stdout="E   assert 1 == 2\n", stderr="1 failed")

        with patch("coord.state.record_test_verdict") as record:
            result = rv.revalidate(
                self._candidates(), _live_config(git_fleet), runner=runner,
            )

        assert result.ok is False
        record.assert_not_called()
        assert "SUITE FAILED" in result.reason
        assert "assert 1 == 2" in result.output
        # The failure is quoted back to the operator, not swallowed.
        rendered = "\n".join(rv.format_failure(result))
        assert "assert 1 == 2" in rendered
        assert "worktree kept for inspection" in rendered

    def test_failing_build_records_nothing(self, git_fleet: Path, coord_db) -> None:
        cfg = _live_config(git_fleet)
        cfg._repo.build_command = "exit 3"

        def runner(command, cwd, timeout):
            return _Run(3 if command == "exit 3" else 0, stderr="boom")

        with patch("coord.state.record_test_verdict") as record:
            result = rv.revalidate(self._candidates(), cfg, runner=runner)

        assert result.ok is False
        assert "BUILD FAILED" in result.reason
        record.assert_not_called()

    def test_timeout_records_nothing(self, git_fleet: Path, coord_db) -> None:
        def runner(command, cwd, timeout):
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

        with patch("coord.state.record_test_verdict") as record:
            result = rv.revalidate(
                self._candidates(), _live_config(git_fleet), runner=runner,
                timeout=5,
            )

        assert result.ok is False
        assert "timed out" in result.reason
        record.assert_not_called()

    def test_unconfigured_test_command_refuses(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """Recording `passed` for a suite that does not exist is never
        correct — refuse instead."""
        with patch("coord.state.record_test_verdict") as record:
            result = rv.revalidate(
                self._candidates(),
                _live_config(git_fleet, test_command=None),
                runner=lambda *a: _Run(0),
            )
        assert result.ok is False
        assert "no test_command" in result.reason
        record.assert_not_called()

    def test_missing_local_checkout_refuses(self, tmp_path: Path, coord_db) -> None:
        with patch("coord.state.record_test_verdict") as record:
            result = rv.revalidate(
                self._candidates(),
                _live_config(tmp_path / "does-not-exist"),
                runner=lambda *a: _Run(0),
            )
        assert result.ok is False
        assert "no local checkout" in result.reason
        record.assert_not_called()

    def test_mixed_bases_are_refused(self, git_fleet: Path, coord_db) -> None:
        """A composite that spans two bases validates nothing meaningful."""
        cands = self._candidates()
        cands[1].entry.target_branch = "develop"
        with patch("coord.state.record_test_verdict") as record:
            result = rv.revalidate(
                cands, _live_config(git_fleet), runner=lambda *a: _Run(0),
            )
        assert result.ok is False
        assert "more than one" in result.reason
        record.assert_not_called()

    def test_empty_candidate_list_is_a_no_op(self, coord_db) -> None:
        result = rv.revalidate([], _live_config(Path("/nonexistent")))
        assert result.ok is True
        assert result.recorded == []


# ══════════════════════════════════════════════════════════════════════════════
# 3a2. #2829: the base-SHA recheck an unattended caller must run before it
#      acts on a lock-free composite's result — `_auto_revalidate_tick`
#      (coord/serve_app.py) is the caller; these tests cover the primitives
#      it relies on: `RevalidationResult.validated_base_sha`,
#      `revalidated_base_still_current`, and `recorded_validated_base_shas`.
# ══════════════════════════════════════════════════════════════════════════════

def _head_sha(repo: Path, ref: str = "main") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=str(repo),
        check=True, capture_output=True, text=True,
    ).stdout.strip()


class TestValidatedBaseShaOnResult:
    """`RevalidationResult.validated_base_sha` must name the actual base a
    run validated against — the anchor #2829's recheck compares later."""

    def test_success_carries_the_base_it_validated(
        self, git_fleet: Path, coord_db,
    ) -> None:
        expected = _head_sha(git_fleet)
        with patch("coord.state.record_test_verdict"), \
             patch("coord.state.record_test_staleness_anchor"):
            result = rv.revalidate(
                TestCompositeRevalidation._candidates(),
                _live_config(git_fleet), runner=lambda *a: _Run(0),
            )
        assert result.ok
        assert result.validated_base_sha == expected

    def test_suite_failure_still_carries_the_base(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """A worktree WAS built and validated against a real base even
        though the suite then failed -- the caller needs that SHA to reason
        about the failure, not just about a success."""
        result = rv.revalidate(
            TestCompositeRevalidation._candidates(), _live_config(git_fleet),
            runner=lambda *a: _Run(1, stderr="boom"),
        )
        assert result.ok is False
        assert result.validated_base_sha == _head_sha(git_fleet)

    def test_setup_failure_never_claims_a_validated_base(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """No worktree was ever built for a SETUP failure -- there is
        nothing to have validated against."""
        result = rv.revalidate(
            TestCompositeRevalidation._candidates(),
            _live_config(git_fleet, test_command=None),
            runner=lambda *a: _Run(0),
        )
        assert result.ok is False
        assert result.kind == rv.KIND_SETUP
        assert result.validated_base_sha is None


class TestRevalidatedBaseStillCurrent:
    """The correctness crux: an unattended caller re-checks this immediately
    before merging anything a lock-free composite validated."""

    def test_true_when_base_unmoved(self, git_fleet: Path, coord_db) -> None:
        cfg = _live_config(git_fleet)
        assert rv.revalidated_base_still_current(
            cfg, "api", "main", _head_sha(git_fleet),
        ) is True

    def test_false_once_a_concurrent_merge_moves_the_base(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """Simulates exactly the race #2829 exists to close: another merge
        landed on `main` while this (lock-free) composite was running."""
        cfg = _live_config(git_fleet)
        validated = _head_sha(git_fleet)

        origin = git_fleet.parent / "origin.git"
        mover = git_fleet.parent / "mover"
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(mover)],
            check=True, capture_output=True, text=True,
        )
        _git(mover, "config", "user.email", "t@example.com")
        _git(mover, "config", "user.name", "t")
        (mover / "concurrent.txt").write_text("a different merge landed\n")
        _git(mover, "add", ".")
        _git(mover, "commit", "-q", "-m", "concurrent merge")
        _git(mover, "push", "-q", "origin", "main")

        assert rv.revalidated_base_still_current(
            cfg, "api", "main", validated,
        ) is False

    def test_fail_closed_on_none_sha(self, git_fleet: Path, coord_db) -> None:
        """`None` means nothing was ever validated (a SETUP result) -- never
        treat that as 'confirmed unchanged'."""
        cfg = _live_config(git_fleet)
        assert rv.revalidated_base_still_current(cfg, "api", "main", None) is False

    def test_fail_closed_on_missing_checkout(self, tmp_path: Path, coord_db) -> None:
        cfg = _live_config(tmp_path / "does-not-exist")
        assert rv.revalidated_base_still_current(
            cfg, "api", "main", "deadbeef",
        ) is False


class TestRecordedValidatedBaseShas:
    """Maps each recorded (merge-eligible) candidate to the SHA the run that
    actually cleared it validated against — the input to the recheck above,
    for both a clean composite and a fallen-back-to-per-entry batch."""

    def test_clean_composite_maps_every_recorded_id_to_the_shared_base(
        self, git_fleet: Path, coord_db,
    ) -> None:
        cands = TestCompositeRevalidation._candidates()
        with patch("coord.state.record_test_verdict"), \
             patch("coord.state.record_test_staleness_anchor"):
            batch = rv.revalidate_group(
                cands, _live_config(git_fleet), runner=lambda *a: _Run(0),
            )
        assert batch.ok
        assert not batch.fell_back
        mapping = rv.recorded_validated_base_shas(batch, cands)
        assert set(mapping) == {"w1", "w2"}
        assert mapping["w1"] == mapping["w2"] == batch.composite.validated_base_sha
        assert mapping["w1"] is not None

    def test_fallback_maps_each_survivor_to_its_own_solo_base(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """Composite (both branches present) fails; each branch passes
        alone -- the #1715 narrowing path. Both survivors were validated,
        just by separate runs, and the mapping must reflect that."""
        cands = TestCompositeRevalidation._candidates()

        def runner(command, cwd, timeout):
            both = (Path(cwd) / "f101.txt").exists() and (Path(cwd) / "f102.txt").exists()
            return _Run(1, stderr="composite red") if both else _Run(0)

        with patch("coord.state.record_test_verdict"), \
             patch("coord.state.record_test_staleness_anchor"):
            batch = rv.revalidate_group(cands, _live_config(git_fleet), runner=runner)

        assert batch.fell_back
        assert sorted(batch.recorded) == ["w1", "w2"]
        mapping = rv.recorded_validated_base_shas(batch, cands)
        assert set(mapping) == {"w1", "w2"}
        for sha in mapping.values():
            assert sha is not None

    def test_unrecorded_candidate_is_absent_from_the_mapping(
        self, git_fleet: Path, coord_db,
    ) -> None:
        cands = TestCompositeRevalidation._candidates()
        with patch("coord.state.record_test_verdict"):
            batch = rv.revalidate_group(
                cands, _live_config(git_fleet),
                runner=lambda *a: _Run(1, stderr="boom"),
            )
        assert batch.recorded == []
        assert rv.recorded_validated_base_shas(batch, cands) == {}


# ══════════════════════════════════════════════════════════════════════════════
# 3a. #1924: the composed-suite subprocess must not inherit `coord serve`'s
#     own daemon-routing guard vars (e.g. `COORD_MERGE_ON_DAEMON`) — a thin
#     client's `--revalidate` is routed to the daemon, whose process has that
#     var set on itself for the very request running the revalidation, and
#     the suite subprocess inherited it by default, failing tests that assert
#     it unset regardless of what the branch under test contains.
# ══════════════════════════════════════════════════════════════════════════════

class TestSuiteEnvironmentScrub:
    def test_shell_runner_scrubs_every_known_daemon_guard(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        for var in rv._DAEMON_GUARD_ENV_VARS:
            monkeypatch.setenv(var, "1")

        check = " && ".join(
            f'test -z "${{{var}:-}}"' for var in rv._DAEMON_GUARD_ENV_VARS
        )
        result = rv._shell_runner(check, tmp_path, 30)

        assert result.returncode == 0, (
            f"a daemon guard var leaked into the suite subprocess: "
            f"{result.stdout} {result.stderr}"
        )

    def test_shell_runner_still_inherits_ordinary_env(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """The scrub is targeted — it must not turn into a blanket
        ``env={}`` that breaks a suite relying on e.g. PATH or a repo's own
        env vars."""
        monkeypatch.setenv("COORD_MERGE_ON_DAEMON", "1")
        monkeypatch.setenv("COORD_REVALIDATE_TEST_MARKER", "present")

        result = rv._shell_runner(
            'test "$COORD_REVALIDATE_TEST_MARKER" = "present"', tmp_path, 30,
        )

        assert result.returncode == 0, result.stderr

    def test_composite_revalidation_suite_never_sees_the_merge_guard(
        self, git_fleet: Path, coord_db, monkeypatch,
    ) -> None:
        """End-to-end, through the real (non-fake) runner: `coord serve`'s
        own ``COORD_MERGE_ON_DAEMON=1`` — set on its process for the very
        `/merge --revalidate` request that reaches this code — must not
        reach the composed suite."""
        monkeypatch.setenv("COORD_MERGE_ON_DAEMON", "1")
        cfg = _live_config(
            git_fleet, test_command='test -z "${COORD_MERGE_ON_DAEMON:-}"',
        )
        candidates = TestCompositeRevalidation._candidates()

        with patch("coord.state.record_test_verdict"):
            result = rv.revalidate(candidates, cfg)  # no `runner=` kwarg

        assert result.ok, result.reason


# ══════════════════════════════════════════════════════════════════════════════
# 3b. #1814: "the suite could not run" is not "the suite failed"
# ══════════════════════════════════════════════════════════════════════════════

class TestInfrastructureFailureClassification:
    """A run that never happened must not be reported as a red suite.

    The daemon that executes revalidation is a systemd user unit whose PATH
    has no `~/.cargo/bin`, so `cargo test` died with "command not found" and
    `--revalidate` printed `SUITE FAILED` for a branch whose six CI checks
    were green. The operator was then sent to debug a branch that was fine —
    and, worse, the documented cure for the stale-verdict cascade silently did
    not work for any Rust branch.

    Reuses the composite tests' `_candidates` so these run against the same
    real git fleet and the same all-or-nothing write contract.
    """

    _candidates = staticmethod(TestCompositeRevalidation._candidates)

    @pytest.mark.parametrize(
        ("returncode", "stdout"),
        [
            # The runner's own report: dedicated exit code AND marker.
            (rv.RUNNER_INFRA_EXIT, "TOOLCHAIN MISSING(rust): 'cargo' not found"),
            # A bare shell "command not found" from an arbitrary test_command.
            (127, "sh: 1: cargo: not found"),
            # Exit code flattened somewhere in between: the marker still classifies.
            (1, "TOOLCHAIN MISSING(rust): 'cargo' not found\nRESULT: INFRA (rust)"),
        ],
    )
    def test_unrunnable_suite_is_infra_not_a_failed_suite(
        self, git_fleet: Path, coord_db, returncode: int, stdout: str,
    ) -> None:
        def runner(command, cwd, timeout):
            return _Run(returncode, stdout=stdout)

        with patch("coord.state.record_test_verdict") as record:
            result = rv.revalidate(
                self._candidates(), _live_config(git_fleet), runner=runner,
            )

        assert result.ok is False
        record.assert_not_called()
        assert result.kind == rv.KIND_INFRA

        # The wording an operator reads must not send them to the branch.
        assert "SUITE FAILED" not in result.reason
        assert "COULD NOT RUN" in result.reason
        rendered = "\n".join(rv.format_failure(result))
        assert "SUITE FAILED" not in rendered
        assert "not a test failure" in rendered.lower()
        # Still fails closed, and still keeps the tree for inspection.
        assert "worktree kept for inspection" in rendered

    def test_a_genuinely_red_suite_is_still_a_red_suite(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """The dangerous direction of this change.

        Misreading a real failure as infrastructure would be far worse than
        the bug it fixes — it is the one that could eventually launder a
        merge. A plain non-zero exit with ordinary test output stays SUITE.
        """
        def runner(command, cwd, timeout):
            return _Run(1, stdout="E   assert 1 == 2\n1 failed", stderr="")

        result = rv.revalidate(
            self._candidates(), _live_config(git_fleet), runner=runner,
        )

        assert result.kind == rv.KIND_SUITE
        assert "SUITE FAILED" in result.reason

    def test_unrunnable_build_is_infra_not_a_failed_build(
        self, git_fleet: Path, coord_db,
    ) -> None:
        cfg = _live_config(git_fleet)
        cfg._repo.build_command = "cargo build"

        def runner(command, cwd, timeout):
            return _Run(127, stderr="sh: 1: cargo: not found")

        result = rv.revalidate(self._candidates(), cfg, runner=runner)

        assert result.kind == rv.KIND_INFRA
        assert "BUILD FAILED" not in result.reason

    def test_infra_never_narrows_to_per_entry_runs(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """N solo re-runs would hit the identical missing toolchain.

        Narrowing here would turn one broken daemon environment into N
        branches that each look individually broken — the precise impression
        this issue exists to remove.
        """
        calls: list[str] = []

        def runner(command, cwd, timeout):
            calls.append(command)
            return _Run(rv.RUNNER_INFRA_EXIT, stdout="TOOLCHAIN MISSING(rust)")

        batch = rv.revalidate_group(
            self._candidates(), _live_config(git_fleet), runner=runner,
        )

        assert batch.ok is False
        assert batch.fell_back is False, "an infra failure must not be narrowed"
        assert batch.per_entry == []
        assert batch.culprits == []
        assert len(calls) == 1
        # No suite ran, so the run cost zero suite runs — the operator's
        # "how many suites did that just run" must not count a no-op.
        assert batch.suite_runs == 0

    def test_operator_summary_says_nothing_was_judged(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """`format_batch` is the stdout half of the report. On an infra
        failure it must state outright that no branch was judged, rather than
        leaving a red composite with no explanation."""
        def runner(command, cwd, timeout):
            return _Run(rv.RUNNER_INFRA_EXIT, stdout="TOOLCHAIN MISSING(rust)")

        batch = rv.revalidate_group(
            self._candidates(), _live_config(git_fleet), runner=runner,
        )
        rendered = "\n".join(rv.format_batch(batch))

        assert "INFRASTRUCTURE FAILURE" in rendered
        assert "no branch was judged" in rendered
        assert "SUITE FAILED" not in rendered
        assert "FAILS alone" not in rendered


class TestIsInfrastructureFailure:
    """Unit coverage for the classifier itself (#1814)."""

    @pytest.mark.parametrize(
        ("rc", "output"),
        [
            (rv.SHELL_NOT_FOUND_EXIT, ""),
            (1, "TOOLCHAIN MISSING(rust): 'cargo' not found"),
            (1, "RESULT: INFRA (rust)"),
            (rv.RUNNER_INFRA_EXIT, "RESULT: INFRA (rust)"),
        ],
    )
    def test_positive_signals(self, rc: int, output: str) -> None:
        assert rv.is_infrastructure_failure(rc, output) is True

    def test_the_runners_exit_code_alone_is_not_enough(self) -> None:
        """A repo's own build/test command may legitimately exit 3.

        This repo's suite has exactly such a case (`build_command = "exit 3"`
        standing in for a real red build). Keying on the number would relabel
        it as infrastructure — the direction that could eventually launder a
        merge — so the marker in the output is what decides.
        """
        assert rv.is_infrastructure_failure(rv.RUNNER_INFRA_EXIT, "boom") is False

    @pytest.mark.parametrize(
        ("rc", "output"),
        [
            (1, "RESULT: FAIL (rust)"),
            (1, "FAILED tests/test_x.py::test_y - AssertionError"),
            # Deliberately narrow: a bare "not found" in ordinary test output
            # (an assertion message, a 404 in a log) is NOT infrastructure.
            (1, "AssertionError: expected 'widget not found' in response"),
            (2, "usage: ..."),
            # #1814 review: this repo's OWN pytest arm now contains tests
            # whose literal assertion text and parametrize IDs embed the
            # markers themselves (test_coord_test_runner_toolchain.py,
            # test_revalidate.py). If one of those specific tests ever fails
            # for an unrelated reason, pytest's failure reporting reproduces
            # the marker text verbatim — but never at the start of a line.
            # A bare substring match would misclassify that genuine, unrelated
            # Python failure as infrastructure and hide it from the operator.
            (
                1,
                "FAILED tests/test_coord_test_runner_toolchain.py::test_x"
                "[TOOLCHAIN MISSING(rust): 'cargo' not found] - AssertionError",
            ),
            (1, "E       assert 'RESULT: INFRA' in out"),
            (
                1,
                "      TOOLCHAIN MISSING(rust) — indented, as the runner's own "
                "`tail -n 40 ... | sed 's/^/      /'` rerun dump always is",
            ),
        ],
    )
    def test_negative_signals(self, rc: int, output: str) -> None:
        assert rv.is_infrastructure_failure(rc, output) is False

    def test_zero_exit_is_never_infrastructure(self) -> None:
        """Only reached on a non-zero exit today, but the classifier must not
        claim a green run could not run."""
        assert rv.is_infrastructure_failure(0, "") is False

    def test_marker_must_be_at_line_start(self) -> None:
        """The runner's `say()` always emits a marker as the first characters
        of a line it prints. A marker appearing mid-line (e.g. inside a
        pytest assertion diff or FAILED summary) is not the runner speaking —
        it is this repo's own test suite quoting the marker as literal text
        (#1814 review). Only a line that genuinely starts with the marker
        counts.
        """
        assert (
            rv.is_infrastructure_failure(
                1, "blah blah RESULT: INFRA (rust) blah"
            )
            is False
        )
        assert (
            rv.is_infrastructure_failure(1, "RESULT: INFRA (rust) blah")
            is True
        )

    def test_infra_is_not_narrowable(self) -> None:
        assert rv.KIND_INFRA not in rv.NARROWABLE_KINDS
        assert rv.KIND_INFRA in rv.NO_SUITE_RAN_KINDS


# ══════════════════════════════════════════════════════════════════════════════
# 3.5. #1851: `_apply_ci_revalidation` — the CI-rerun arm of --revalidate
# ══════════════════════════════════════════════════════════════════════════════
#
# Unlike `_apply_revalidation` (the local-suite arm exercised end to end in
# section 4 below), this arm's remedy is a `gh run rerun` — no worktree, no
# local suite, no board write. Exercised directly against
# `coord.commands.merge._apply_ci_revalidation` with fakes, rather than
# through the full CLI/git-fleet black-box, since there is no local git state
# for it to touch.

class _CiRerunFake:
    """Green checks whose `started_at` predates the base commit time — the
    #1851 staleness signal — with a spy on `rerun_for_pr`."""

    def __init__(self, *, checks_started_at: float | None = 500.0, rerun_ok: bool = True):
        self.is_available = True
        self.checks_started_at = checks_started_at
        self.rerun_ok = rerun_ok
        self.rerun_calls: list[tuple[str, int]] = []

    def list_checks_for_pr(self, repo, number):
        from types import SimpleNamespace
        return [SimpleNamespace(
            name="build", status="completed", conclusion="success",
            started_at=self.checks_started_at, completed_at=None,
        )]

    def rerun_for_pr(self, repo, number):
        self.rerun_calls.append((repo, number))
        return self.rerun_ok


class _GhBranchTimestamp:
    def __init__(self, ts: float | None = 1000.0):
        self.ts = ts

    def get_branch_commit_timestamp(self, repo, branch):
        return self.ts


class TestApplyCiRevalidation:
    @staticmethod
    def _entry_with_pr(pr: int | None):
        e = _entry("w1", issue=101)
        e.pr_number = pr
        return e

    def test_triggers_rerun_for_a_ci_stale_entry(self, capsys) -> None:
        from coord.commands.merge import _apply_ci_revalidation

        items = [self._entry_with_pr(501)]
        ci = _CiRerunFake()
        cfg = _config(gates=["merge"], reviews=False)

        _apply_ci_revalidation(
            items, Board(active=[], completed=[]), cfg, ci, _GhBranchTimestamp(),
            dry_run=False,
        )

        assert ci.rerun_calls == [("acme/api", 501)]
        out = capsys.readouterr().out
        assert "triggered a CI re-run" in out
        assert "PR #501" in out

    def test_dry_run_names_it_without_triggering(self, capsys) -> None:
        from coord.commands.merge import _apply_ci_revalidation

        items = [self._entry_with_pr(501)]
        ci = _CiRerunFake()
        cfg = _config(gates=["merge"], reviews=False)

        _apply_ci_revalidation(
            items, Board(active=[], completed=[]), cfg, ci, _GhBranchTimestamp(),
            dry_run=True,
        )

        assert ci.rerun_calls == [], "dry-run must not trigger a real rerun"
        out = capsys.readouterr().out
        assert "would re-run CI" in out
        assert "PR #501" in out

    def test_nothing_stale_says_so_and_triggers_nothing(self, capsys) -> None:
        from coord.commands.merge import _apply_ci_revalidation

        items = [self._entry_with_pr(501)]
        # Checks postdate the base — fresh, not stale.
        ci = _CiRerunFake(checks_started_at=1500.0)
        cfg = _config(gates=["merge"], reviews=False)

        _apply_ci_revalidation(
            items, Board(active=[], completed=[]), cfg, ci, _GhBranchTimestamp(),
            dry_run=False,
        )

        assert ci.rerun_calls == []
        out = capsys.readouterr().out
        assert "no entry is blocked solely on stale CI checks" in out

    def test_ci_store_none_is_inert(self, capsys) -> None:
        from coord.commands.merge import _apply_ci_revalidation

        items = [self._entry_with_pr(501)]
        cfg = _config(gates=["merge"], reviews=False)

        _apply_ci_revalidation(
            items, Board(active=[], completed=[]), cfg, None, _GhBranchTimestamp(),
            dry_run=False,
        )
        # No exception, no output claiming anything was found/triggered.
        out = capsys.readouterr().out
        assert "triggered" not in out

    def test_partial_rerun_failure_is_reported(self, capsys) -> None:
        from coord.commands.merge import _apply_ci_revalidation

        items = [self._entry_with_pr(501)]
        ci = _CiRerunFake(rerun_ok=False)
        cfg = _config(gates=["merge"], reviews=False)

        _apply_ci_revalidation(
            items, Board(active=[], completed=[]), cfg, ci, _GhBranchTimestamp(),
            dry_run=False,
        )

        assert ci.rerun_calls == [("acme/api", 501)]
        captured = capsys.readouterr()
        # `click.echo(..., err=True)` for the failure line — checked on stderr.
        assert "could not trigger a CI re-run" in captured.err


class _CiRerunSettleFake:
    """#1925: like `_CiRerunFake` (green-but-stale pre-rerun, spied
    `rerun_for_pr`), except `list_checks_for_pr` switches to *post_rerun*
    (a scripted sequence, last entry repeats) once `rerun_for_pr` has been
    called — so tests can drive `_apply_ci_revalidation`'s post-trigger
    settle-wait deterministically."""

    def __init__(self, post_rerun: list[list]):
        self.is_available = True
        self.post_rerun = post_rerun
        self.rerun_calls: list[tuple[str, int]] = []
        self._reads_since_rerun = 0

    def list_checks_for_pr(self, repo, number):
        from types import SimpleNamespace

        if not self.rerun_calls:
            # Pre-rerun: green, but started well before the mocked base
            # commit time (1000.0) — the #1851 staleness signal that makes
            # this entry a candidate in the first place.
            return [SimpleNamespace(
                name="build", status="completed", conclusion="success",
                started_at=500.0, completed_at=None,
            )]
        i = min(self._reads_since_rerun, len(self.post_rerun) - 1)
        self._reads_since_rerun += 1
        return self.post_rerun[i]

    def rerun_for_pr(self, repo, number):
        self.rerun_calls.append((repo, number))
        return True

    def invalidate(self, repo, number):
        pass


def _unreadable_ns(repo="acme/api", number=501):
    from types import SimpleNamespace
    return SimpleNamespace(
        name=f"coord: could not read CI status for {repo}#{number} (gh pr "
        "checks failed: no checks reported)",
        status="completed", conclusion="unknown",
        started_at=None, completed_at=None,
    )


def _green_ns():
    from types import SimpleNamespace
    return SimpleNamespace(
        name="build", status="completed", conclusion="success",
        started_at=1500.0, completed_at=None,
    )


def _failed_ns():
    from types import SimpleNamespace
    return SimpleNamespace(
        name="build", status="completed", conclusion="failure",
        started_at=1500.0, completed_at=None,
    )


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TestApplyCiRevalidationSettleWait:
    """#1925: the registration-gap regression itself — triggering a CI
    re-run and immediately reading `gh pr checks` used to see the #1525
    synthetic `unknown` conclusion and block exactly like genuinely broken
    CI. These drive `_apply_ci_revalidation` end to end (past the trigger,
    through the settle wait) with a fake clock so no test sleeps for real.
    """

    @staticmethod
    def _entry_with_pr(pr: int):
        e = _entry("w1", issue=101)
        e.pr_number = pr
        return e

    def test_settles_quickly_is_not_deferred(self, capsys) -> None:
        from coord.commands.merge import _apply_ci_revalidation

        items = [self._entry_with_pr(501)]
        ci = _CiRerunSettleFake(post_rerun=[[_unreadable_ns()], [_green_ns()]])
        cfg = _config(gates=["merge"], reviews=False)
        clk = _FakeClock()

        deferred = _apply_ci_revalidation(
            items, Board(active=[], completed=[]), cfg, ci, _GhBranchTimestamp(),
            dry_run=False, poll_sleep=clk.sleep, poll_clock=clk.clock,
        )

        assert deferred == set()
        assert items[0].error is None
        out = capsys.readouterr().out
        assert "settled after" in out
        assert "checks_failed" not in out

    def test_never_registers_defers_instead_of_blocking(self, capsys) -> None:
        """The reproduced #1925 shape: the re-run never registers within the
        wait budget. Must come back as `deferred`, not as a `checks_failed`-
        shaped block, and the message must say plainly that THIS command
        caused the transient reading."""
        from coord.commands.merge import _apply_ci_revalidation

        items = [self._entry_with_pr(501)]
        ci = _CiRerunSettleFake(post_rerun=[[_unreadable_ns()]])
        cfg = _config(gates=["merge"], reviews=False)
        clk = _FakeClock()

        deferred = _apply_ci_revalidation(
            items, Board(active=[], completed=[]), cfg, ci, _GhBranchTimestamp(),
            dry_run=False, poll_sleep=clk.sleep, poll_clock=clk.clock,
        )

        assert deferred == {"w1"}
        entry = items[0]
        assert entry.state == mq.PENDING, "must stay pending, never blocked terminally"
        assert entry.error is not None
        assert "not a CI failure" in entry.error
        assert "--revalidate" in entry.error
        out = capsys.readouterr().out
        assert "checks_failed" not in out

    def test_genuinely_red_after_rerun_is_not_deferred(self, capsys) -> None:
        """A real failure discovered by the re-run must settle and be left
        for the merge gate to block on for its own (real) reason — #1925
        only defers the self-caused registration gap, never a genuine
        result, good or bad."""
        from coord.commands.merge import _apply_ci_revalidation

        items = [self._entry_with_pr(501)]
        ci = _CiRerunSettleFake(post_rerun=[[_failed_ns()]])
        cfg = _config(gates=["merge"], reviews=False)
        clk = _FakeClock()

        deferred = _apply_ci_revalidation(
            items, Board(active=[], completed=[]), cfg, ci, _GhBranchTimestamp(),
            dry_run=False, poll_sleep=clk.sleep, poll_clock=clk.clock,
        )

        assert deferred == set()
        assert items[0].error is None
        out = capsys.readouterr().out
        assert "settled after" in out


# ══════════════════════════════════════════════════════════════════════════════
# 4. Black-box: the CLI, end to end
# ══════════════════════════════════════════════════════════════════════════════

def _live_sha(checkout: Path):
    """Answer `get_branch_sha` from the REAL git refs in *checkout*.

    The staleness the black-box exercises is seeded on the board side (the
    recorded anchors name commits that no longer describe anything), while the
    *live* side reads the actual repo — so after `--revalidate` re-anchors a
    verdict to the commits it really validated, the gate genuinely agrees. A
    fake on both sides would prove nothing about the round trip.
    """
    def _get(repo, branch):
        res = subprocess.run(
            ["git", "rev-parse", f"origin/{branch}"],
            cwd=str(checkout), capture_output=True, text=True,
        )
        return res.stdout.strip() or None
    return _get


def _compare_files(repo, base, head):
    # The base move touched real source → never inert (#1738), so a verdict
    # anchored to the old base is genuinely stale.
    return ["coord/merge_queue.py"]


_FLEET = (("w1", 101), ("w2", 102), ("w3", 103))


def _seed_stale_entries(pairs=_FLEET[:2]):
    """N approved, tested entries whose verdicts were all staled by a base
    move — the #1769/#1715 headline scenario, with no live drive."""
    from coord.state import save_board

    entries = []
    works = []
    for aid, issue in pairs:
        e = _entry(aid, issue=issue)
        e.branch_head_sha = f"branch-sha-{issue}"
        entries.append(e)
        works.append(_tested_work(aid, issue=issue))
    mq.save_queue(entries)
    save_board(Board(active=[], completed=works))
    for aid, issue in pairs:
        _stamp_anchors(
            aid,
            head_sha=f"branch-sha-{issue}",
            base_sha="base-old",          # main has since moved to base-new
            patch_id=f"patch-{issue}",
        )
    return entries


def _seed_two_stale_entries(git_fleet: Path):
    return _seed_stale_entries(_FLEET[:2])


@pytest.fixture
def blackbox(git_fleet: Path, tmp_path: Path, coord_db):
    """Config + checkout for the headline scenario: two approved, tested
    entries, base moved so both verdicts are stale, no live drive."""
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(CONFIG_YAML.format(repo_path=str(git_fleet)))
    _seed_two_stale_entries(git_fleet)
    return cfg, git_fleet


def _gh_patches(checkout: Path):
    """Patch the `gh` surface `coord merge` touches so nothing hits the network."""
    next_pr = [900]

    def fake_create_pr(repo, *, base, head, title, body):
        n = next_pr[0]
        next_pr[0] += 1
        return {"number": n, "url": f"u/{n}", "existed": False}

    return [
        patch("coord.github_ops.create_pr", side_effect=fake_create_pr),
        patch("coord.github_ops.get_pr_size", return_value=10),
        patch("coord.github_ops.merge_pr", return_value=(True, "ok")),
        patch("coord.github_ops.get_branch_sha", side_effect=_live_sha(checkout)),
        patch("coord.github_ops.get_branch_patch_id", return_value=None),
        patch("coord.github_ops.get_compare_files", side_effect=_compare_files),
        patch("coord.github_ops.list_remote_branch_names", return_value=set()),
    ]


def _invoke(args: list[str], checkout: Path):
    stack = _gh_patches(checkout)
    for p in stack:
        p.start()
    try:
        return CliRunner().invoke(main, args)
    finally:
        for p in reversed(stack):
            p.stop()


def _states() -> dict[str, str]:
    return {x.assignment_id: x.state for x in mq.load_queue()}


class TestMergeRevalidateBlackBox:
    """#1769 acceptance: "Black-box test: seeded board, two approved entries,
    base moved so both verdicts are stale; assert `--revalidate` drains both
    and that plain `coord merge` drains neither."
    """

    def test_plain_merge_drains_neither_and_never_re_tests(self, blackbox) -> None:
        cfg, checkout = blackbox
        with patch("coord.revalidate.revalidate") as reval:
            result = _invoke(["merge", "--config", str(cfg)], checkout)

        assert result.exit_code == 0, result.output
        reval.assert_not_called()  # plain `coord merge` must never re-test
        assert _states() == {"w1": mq.PENDING, "w2": mq.PENDING}
        assert "stale" in result.output.lower()

    def test_dry_run_names_both_as_candidates_without_running_anything(
        self, blackbox,
    ) -> None:
        """#1715: "--dry-run names the batch members and states plainly that
        one composed run will validate all of them"."""
        cfg, checkout = blackbox
        with patch("coord.revalidate.revalidate") as reval:
            result = _invoke(
                ["merge", "--config", str(cfg), "--revalidate", "--dry-run"],
                checkout,
            )

        assert result.exit_code == 0, result.output
        reval.assert_not_called()
        # Every member is named...
        assert "revalidate: api #101 (issue-101-w1" in result.output
        assert "revalidate: api #102 (issue-102-w2" in result.output
        # ...as ONE batch, costing ONE run.
        assert "BATCH of 2" in result.output
        assert "ONE composed suite run (not 2)" in result.output
        assert "2 entry(ies) in 1 batch(es) — 1 suite run(s)" in result.output
        # The trade is stated where the operator decides, not just in --help.
        assert "validates the COMPOSITE, not each branch alone" in result.output
        assert _states() == {"w1": mq.PENDING, "w2": mq.PENDING}

    def test_revalidate_drains_both(self, blackbox) -> None:
        """The headline criterion: two approved, tested branches queued with no
        live drive — `coord merge --revalidate` merges both, no human action."""
        cfg, checkout = blackbox
        result = _invoke(["merge", "--config", str(cfg), "--revalidate"], checkout)

        assert result.exit_code == 0, result.output
        assert "--revalidate: PASSED" in result.output
        assert _states() == {"w1": mq.MERGED, "w2": mq.MERGED}, result.output

    def test_failing_revalidation_merges_nothing(self, blackbox) -> None:
        """A composite that fails against the current base leaves BOTH entries
        blocked. Never a laundering path."""
        cfg, checkout = blackbox
        cfg.write_text(
            cfg.read_text().replace('test_command: "true"', 'test_command: "exit 1"')
        )

        result = _invoke(["merge", "--config", str(cfg), "--revalidate"], checkout)

        assert "SUITE FAILED" in result.output
        assert _states() == {"w1": mq.PENDING, "w2": mq.PENDING}
        assert mq.MERGED not in _states().values()

    def test_revalidate_with_nothing_stale_says_so(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """--revalidate on a queue with no stale entry is inert and explicit."""
        from coord.state import save_board

        cfg = tmp_path / "coordinator.yml"
        cfg.write_text(CONFIG_YAML.format(repo_path=str(git_fleet)))
        mq.save_queue([_entry("w1", issue=101)])
        # No verdict at all → SMOKE_MISSING, which --revalidate never touches.
        save_board(Board(active=[], completed=[
            _tested_work("w1", issue=101, test_state=None),
        ]))

        with patch("coord.revalidate.revalidate") as reval:
            result = _invoke(
                ["merge", "--config", str(cfg), "--revalidate"], git_fleet,
            )

        reval.assert_not_called()
        assert "no entry is blocked solely on a stale test verdict" in result.output
        assert _states() == {"w1": mq.PENDING}


class TestBatchAcceptance:
    """#1715: the cascade half. N approved branches on one base used to cost
    N−1 full suite runs, because the first merge staled everything behind it.

    These are the issue's stated acceptance criteria, asserted on the run
    COUNT directly — "this is the whole point and it must be asserted
    directly, not implied by wall-clock".
    """

    @staticmethod
    def _cfg_with_counter(tmp_path: Path, git_fleet: Path, tail: str = "") -> tuple:
        """Config whose test_command appends one byte per invocation.

        Counting invocations of the repo's real `test_command`, through the
        real CLI, is the only measurement that actually answers "how many
        suite runs did that cost" — a mock of `revalidate()` would move the
        assertion above the thing under test.
        """
        counter = tmp_path / "suite-runs"
        cmd = f"printf x >> {counter}" + tail
        cfg = tmp_path / "coordinator.yml"
        cfg.write_text(
            CONFIG_YAML.format(repo_path=str(git_fleet)).replace(
                'test_command: "true"', f'test_command: "{cmd}"',
            )
        )
        return cfg, counter

    @staticmethod
    def _runs(counter: Path) -> int:
        return len(counter.read_text()) if counter.exists() else 0

    def test_three_stale_entries_cost_one_suite_run_and_all_merge(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """THE headline criterion: "Three approved entries queued, all
        stale-but-`passed`, base moved: `coord merge --revalidate` performs ONE
        suite run and merges all three. Assert the run count is 1, not 3."
        """
        _seed_stale_entries(_FLEET)
        cfg, counter = self._cfg_with_counter(tmp_path, git_fleet)

        result = _invoke(["merge", "--config", str(cfg), "--revalidate"], git_fleet)

        assert result.exit_code == 0, result.output
        assert self._runs(counter) == 1, (
            f"expected ONE composed suite run for three entries, got "
            f"{self._runs(counter)} — the cascade is back\n{result.output}"
        )
        assert _states() == {
            "w1": mq.MERGED, "w2": mq.MERGED, "w3": mq.MERGED,
        }, result.output
        assert "--revalidate: PASSED" in result.output
        assert "1 suite run(s) for 3 entry(ies)" in result.output

    def test_red_composite_merges_nothing_then_narrows_to_the_culprit(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """#1715: "A red composite merges nothing, and the follow-up per-entry
        pass identifies the actual culprit and merges the others."

        The suite here fails iff issue 103's file is in the tree, so the
        composite (which contains it) is red while 101 and 102 are green on
        their own — one culprit, two innocents, decided by the real suite
        rather than by a stubbed verdict.
        """
        _seed_stale_entries(_FLEET)
        cfg, counter = self._cfg_with_counter(
            tmp_path, git_fleet, tail="; ! test -f f103.txt",
        )

        result = _invoke(["merge", "--config", str(cfg), "--revalidate"], git_fleet)

        assert result.exit_code == 0, result.output
        # The composite failed → it merged nothing on its own result.
        assert "SUITE FAILED" in result.output
        # ...and the innocents still merged, off their own solo runs.
        assert _states() == {
            "w1": mq.MERGED, "w2": mq.MERGED, "w3": mq.PENDING,
        }, result.output
        # The culprit is NAMED, not just left blocked.
        assert "api #103 (issue-103-w3)" in result.output
        assert "culprit(s): api #103 (issue-103-w3)" in result.output
        # Worst case is 1 composite + N solo, and no more.
        assert self._runs(counter) == 4, result.output
        # #1715 is explicit that a red composite marks nothing failed: the
        # culprit is left PENDING and retryable, not parked in a terminal
        # state that needs a human to unwind.
        assert not ({mq.CONFLICT, mq.HUMAN_REQUIRED, mq.SKIPPED}
                    & set(_states().values()))

    def test_culprit_alone_blocks_only_itself_and_costs_nothing_extra(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """N=1 must stay byte-identical to #1769: one run, no fallback.

        With a single candidate the "composite" already IS that branch, so
        re-running it solo would be the same run twice.
        """
        _seed_stale_entries((("w3", 103),))
        cfg, counter = self._cfg_with_counter(
            tmp_path, git_fleet, tail="; ! test -f f103.txt",
        )

        result = _invoke(["merge", "--config", str(cfg), "--revalidate"], git_fleet)

        assert _states() == {"w3": mq.PENDING}, result.output
        assert self._runs(counter) == 1, "N=1 must not re-run itself"
        assert "re-running each branch on its own" not in result.output

    def test_setup_failure_never_fans_out_into_n_identical_failures(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """A common-mode failure (here: no local checkout) must not trigger the
        per-entry pass — every solo run would hit the identical wall, turning
        one clear error into N copies of it."""
        _seed_stale_entries(_FLEET)
        cfg = tmp_path / "coordinator.yml"
        cfg.write_text(
            CONFIG_YAML.format(repo_path=str(tmp_path / "gone"))
        )

        calls: list[int] = []
        real = rv.revalidate

        def counting(cands, *a, **kw):
            calls.append(len(cands))
            return real(cands, *a, **kw)

        with patch("coord.revalidate.revalidate", side_effect=counting):
            result = _invoke(
                ["merge", "--config", str(cfg), "--revalidate"], git_fleet,
            )

        assert calls == [3], f"expected one composite attempt only, got {calls}"
        assert "no local checkout" in result.output
        assert _states() == {
            "w1": mq.PENDING, "w2": mq.PENDING, "w3": mq.PENDING,
        }

    def test_dry_run_names_the_three_batch_members_and_the_single_run(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """#1715: "--dry-run names the batch members and states plainly that
        one composed run will validate all of them"."""
        _seed_stale_entries(_FLEET)
        cfg, counter = self._cfg_with_counter(tmp_path, git_fleet)

        result = _invoke(
            ["merge", "--config", str(cfg), "--revalidate", "--dry-run"],
            git_fleet,
        )

        assert "revalidate: api #101 (issue-101-w1" in result.output
        assert "revalidate: api #102 (issue-102-w2" in result.output
        assert "revalidate: api #103 (issue-103-w3" in result.output
        assert "BATCH of 3" in result.output
        assert "ONE composed suite run (not 3)" in result.output
        assert self._runs(counter) == 0, "--dry-run must run no suite at all"
        assert _states() == {
            "w1": mq.PENDING, "w2": mq.PENDING, "w3": mq.PENDING,
        }

    def test_culprit_solo_worktree_is_surfaced_in_the_outcome_lines(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """#1715-review: `solo.worktree` (kept for inspection, per
        `revalidate()`'s contract) used to be dropped on the floor —
        `format_failure`, which prints the "worktree kept for inspection"
        line, only ran on the composite's own failure. An operator debugging
        the actual culprit branch needs a pointer to ITS OWN composed
        worktree, not just the (different) composite one."""
        _seed_stale_entries(_FLEET)
        cfg, counter = self._cfg_with_counter(
            tmp_path, git_fleet, tail="; ! test -f f103.txt",
        )

        result = _invoke(["merge", "--config", str(cfg), "--revalidate"], git_fleet)

        assert result.exit_code == 0, result.output
        # The composite's own kept-for-inspection line (existing behaviour).
        # Plus the culprit's OWN solo worktree — this is the new assertion.
        assert result.output.count("worktree kept for inspection:") == 2, (
            "expected one 'kept for inspection' line for the composite and "
            f"one for the solo culprit run\n{result.output}"
        )
        assert "revalidate-worktrees" in result.output


class TestSkipSmokeUnchanged:
    """#1769 acceptance: "--skip-smoke keeps working unchanged as the manual
    override." It waives the gate; --revalidate satisfies it. They are not the
    same thing, and --revalidate must not have altered the waiver."""

    def test_skip_smoke_still_merges_a_stale_entry_without_re_testing(
        self, blackbox,
    ) -> None:
        cfg, checkout = blackbox
        with patch("coord.revalidate.revalidate") as reval:
            result = _invoke(
                ["merge", "--config", str(cfg), "--skip-smoke"], checkout,
            )

        assert result.exit_code == 0, result.output
        reval.assert_not_called()
        assert "--skip-smoke: interactive smoke-test gate bypassed" in result.output
        assert _states() == {"w1": mq.MERGED, "w2": mq.MERGED}


class TestOnlyPathRevalidates:
    """`--only` is the surgical single-entry lane. #1769 covers it too — it is
    the form an operator reaches for when one specific branch has gone stale
    (which is exactly what happened on #1732 and #1703)."""

    def test_only_revalidate_merges_the_one_stale_entry(self, blackbox) -> None:
        cfg, checkout = blackbox
        result = _invoke(
            ["merge", "--config", str(cfg), "--only", "w1", "--revalidate"],
            checkout,
        )

        assert result.exit_code == 0, result.output
        assert "--revalidate: PASSED" in result.output
        states = _states()
        assert states["w1"] == mq.MERGED, result.output
        # The sibling was never in scope for a --only run.
        assert states["w2"] == mq.PENDING

    def test_only_without_revalidate_leaves_it_blocked(self, blackbox) -> None:
        cfg, checkout = blackbox
        with patch("coord.revalidate.revalidate") as reval:
            result = _invoke(
                ["merge", "--config", str(cfg), "--only", "w1"], checkout,
            )

        reval.assert_not_called()
        assert result.exit_code == 0, result.output
        assert _states() == {"w1": mq.PENDING, "w2": mq.PENDING}


class _FakeTimeModule:
    """Drop-in replacement for the stdlib `time` module as seen by
    `coord.ci_store.wait_for_ci_settle` — `sleep` advances the same counter
    `monotonic` reads, so a bounded poll loop runs to completion with no
    real wall-clock wait, however many polls the default budget allows."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


class TestMergeRevalidateCiSettleBlackBox:
    """#1925 acceptance, end to end through the real CLI: a branch whose
    local Test verdict AND CI checks are both stale (the exact #1551/#532
    reproduction — a base move stales both signals at once) must merge in
    ONE `coord merge --revalidate` invocation when the re-run it triggers is
    genuinely healthy, must still block — with the real reason, never the
    self-inflicted "unknown" — when it's genuinely red, and must come back
    as a legible "come back shortly" (never `checks_failed`) when the re-run
    just hasn't registered within the wait budget yet.
    """

    @staticmethod
    def _cfg(tmp_path: Path, checkout: Path) -> Path:
        cfg = tmp_path / "coordinator.yml"
        cfg.write_text(
            CONFIG_YAML.replace("type: none", "type: github").format(
                repo_path=str(checkout)
            )
        )
        return cfg

    @staticmethod
    def _seed_with_pr(pr_number: int):
        entries = _seed_stale_entries((("w1", 101),))
        entries[0].pr_number = pr_number
        mq.save_queue(entries)
        return entries[0]

    def test_stale_verdict_plus_settling_green_ci_merges_in_one_shot(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        self._seed_with_pr(501)
        cfg = self._cfg(tmp_path, git_fleet)
        ci = _CiRerunSettleFake(post_rerun=[[_unreadable_ns()], [_green_ns()]])

        with patch("coord.ci_store.build_ci_store", return_value=ci), \
             patch("coord.github_ops.get_branch_commit_timestamp", return_value=1000.0), \
             patch("coord.ci_store.time", _FakeTimeModule()):
            result = _invoke(
                ["merge", "--config", str(cfg), "--only", "w1", "--revalidate"],
                git_fleet,
            )

        assert result.exit_code == 0, result.output
        assert ci.rerun_calls == [("acme/api", 501)]
        assert "triggered a CI re-run" in result.output
        assert "settled after" in result.output
        assert _states()["w1"] == mq.MERGED, result.output

    def test_stale_verdict_plus_genuinely_red_ci_stays_blocked_with_real_reason(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        self._seed_with_pr(501)
        cfg = self._cfg(tmp_path, git_fleet)
        ci = _CiRerunSettleFake(post_rerun=[[_failed_ns()]])

        with patch("coord.ci_store.build_ci_store", return_value=ci), \
             patch("coord.github_ops.get_branch_commit_timestamp", return_value=1000.0), \
             patch("coord.ci_store.time", _FakeTimeModule()):
            result = _invoke(
                ["merge", "--config", str(cfg), "--only", "w1", "--revalidate"],
                git_fleet,
            )

        assert result.exit_code == 0, result.output
        assert _states()["w1"] == mq.PENDING, result.output
        assert "checks failed" in result.output
        # The block must be the REAL failure this re-run found, not the
        # self-triggered #1925 registration-gap message.
        assert "not a CI failure" not in result.output

    def test_stale_verdict_plus_rerun_that_never_registers_defers_cleanly(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        """The exact #1551/#532 reproduction: the re-run never settles
        within the wait budget. Must be reported as pending/deferred, never
        as `checks_failed`, and the entry must stay PENDING (mergeable on a
        later invocation once CI catches up) rather than parked as broken.
        """
        self._seed_with_pr(501)
        cfg = self._cfg(tmp_path, git_fleet)
        ci = _CiRerunSettleFake(post_rerun=[[_unreadable_ns()]])

        with patch("coord.ci_store.build_ci_store", return_value=ci), \
             patch("coord.github_ops.get_branch_commit_timestamp", return_value=1000.0), \
             patch("coord.ci_store.time", _FakeTimeModule()):
            result = _invoke(
                ["merge", "--config", str(cfg), "--only", "w1", "--revalidate"],
                git_fleet,
            )

        assert result.exit_code == 0, result.output
        assert _states()["w1"] == mq.PENDING, result.output
        assert "checks_failed" not in result.output
        assert "not a CI failure" in result.output
        assert "THIS command started" in result.output


class TestDaemonRoute:
    """The daemon `/merge` route is the lane a thin client (and the TUI 'Go'
    button) reaches. It must forward `--revalidate` — the suite has to run
    where the repo is checked out, which is the daemon host — and the
    unattended auto-drain must never set it."""

    def test_post_merge_forwards_revalidate(
        self, valid_config_path: Path, tmp_path: Path,
    ) -> None:
        from starlette.testclient import TestClient

        from coord.config import load as load_config
        from coord.dao import SqliteStore
        from coord.serve_app import build_app

        seen: dict = {}

        def _fake_callback(**kwargs):
            seen.update(kwargs)

        cfg = load_config(valid_config_path)
        app = build_app(SqliteStore(tmp_path / "daemon.db"), cfg)
        with patch("coord.cli.merge") as merge_cmd:
            merge_cmd.callback = _fake_callback
            with TestClient(app) as cli:
                resp = cli.post(
                    "/merge",
                    json={"dry_run": True, "repo_filter": "no-such", "revalidate": True},
                )
        assert resp.status_code == 200, resp.text
        assert seen.get("revalidate") is True, seen

    def test_post_merge_defaults_revalidate_off(
        self, valid_config_path: Path, tmp_path: Path,
    ) -> None:
        from starlette.testclient import TestClient

        from coord.config import load as load_config
        from coord.dao import SqliteStore
        from coord.serve_app import build_app

        seen: dict = {}

        def _fake_callback(**kwargs):
            seen.update(kwargs)

        cfg = load_config(valid_config_path)
        app = build_app(SqliteStore(tmp_path / "daemon.db"), cfg)
        with patch("coord.cli.merge") as merge_cmd:
            merge_cmd.callback = _fake_callback
            with TestClient(app) as cli:
                resp = cli.post(
                    "/merge", json={"dry_run": True, "repo_filter": "no-such"},
                )
        assert resp.status_code == 200, resp.text
        assert seen.get("revalidate") is False, seen

    def test_auto_drain_never_revalidates(self) -> None:
        """The unattended tick must not start suite runs on its own schedule —
        the 2026-06-07 token-burn shape. `_auto_drain_tick` calls
        `merge_queue.process` directly and never goes near `revalidate`."""
        import inspect

        from coord import serve_app

        src = inspect.getsource(serve_app._auto_drain_tick)
        assert "coord.revalidate" not in src
        assert "revalidate(" not in src


class TestThinClientTimeout:
    """A `--revalidate` run executes the whole suite on the daemon host. The
    thin client's HTTP timeout has to outlast that, or the operator sees a
    timeout error for a merge that actually succeeded.

    #1715-review: the batch cascade's worst case is 1 (composite) + N
    (per-entry fallback) *serial* suite runs, not just one — #1769's padding
    only covered a single run and would already be blown by a four-branch
    red-composite fallback (5 runs, ~35-40 min against a 35 min budget)."""

    def test_revalidate_gets_a_longer_daemon_timeout(self) -> None:
        from coord.commands import merge as merge_mod

        seen: dict = {}

        def fake_post_record(svc, path, params, timeout):
            seen["timeout"] = timeout
            return {"output": "", "exit_code": 0}

        with patch("coord.client.post_record", side_effect=fake_post_record):
            merge_mod._merge_via_daemon(object(), {"revalidate": True})
        assert seen["timeout"] > rv.DEFAULT_TIMEOUT_SECONDS

        with patch("coord.client.post_record", side_effect=fake_post_record):
            merge_mod._merge_via_daemon(object(), {"revalidate": False})
        assert seen["timeout"] == 900.0

    def test_revalidate_timeout_covers_the_documented_1_plus_n_worst_case(
        self,
    ) -> None:
        """The #1715 issue's own headline scenario — a four-branch parallel
        group whose composite goes red — falls back to 5 serial suite runs.
        The client timeout must comfortably outlast that, and in general must
        scale with `coord.revalidate.MAX_REVALIDATION_BATCH`, not just add a
        flat, single-run pad on top of `DEFAULT_TIMEOUT_SECONDS`."""
        from coord.ci_store import CI_RERUN_MAX_WAIT_SECONDS

        headline_worst_case = rv.DEFAULT_TIMEOUT_SECONDS * 5
        assert rv.client_timeout_seconds(True) > headline_worst_case

        assert rv.client_timeout_seconds(True) == (
            rv.DEFAULT_TIMEOUT_SECONDS * (1 + rv.MAX_REVALIDATION_BATCH)
            + CI_RERUN_MAX_WAIT_SECONDS * rv.MAX_REVALIDATION_BATCH
            + 300.0
        )
        assert rv.client_timeout_seconds(False) == 900.0


class TestBaselineRedClassification:
    """A suite that was ALREADY red on the merge-base must not be reported as a
    red suite for the branches (#2170).

    Sibling of `TestInfrastructureFailureClassification` above, and the same
    shape of mistake one rung along. #1814's case was "the suite never ran";
    this one is "the suite ran, failed, and would have failed identically
    without any of these branches". Both render as `SUITE FAILED` if nobody
    distinguishes them, and both send the operator to debug a branch that is
    fine.

    What it cost while undistinguished: six tests failed on `origin/main` on
    any machine with a populated `$HOME`, so the Test stage on `precision`
    could not produce a green verdict for this repo on ANY branch — every
    dispatch there was blamed on the branch and cost a human adjudication.

    Reuses the composite tests' `_candidates` so these run against the same
    real git fleet and the same all-or-nothing write contract.
    """

    _candidates = staticmethod(TestCompositeRevalidation._candidates)

    @staticmethod
    def _baseline_red_runner(command, cwd, timeout):
        return _Run(
            rv.RUNNER_BASELINE_RED_EXIT,
            stdout=(
                "FAIL(python): 6 test(s) fail on re-run — genuine\n"
                "RESULT: BASELINE-RED (python) — every failure reproduces on "
                "origin/main in this environment\n"
            ),
            stderr="",
        )

    def test_a_red_baseline_is_not_a_red_suite(
        self, git_fleet: Path, coord_db,
    ) -> None:
        result = rv.revalidate(
            self._candidates(), _live_config(git_fleet),
            runner=self._baseline_red_runner,
        )

        assert result.kind == rv.KIND_BASELINE_RED
        # The wording must not send the operator to the branch, and must not
        # claim the suite never ran either — it ran, and told us something.
        assert "SUITE FAILED" not in result.reason
        assert "RED BASELINE" in result.reason
        assert "COULD NOT RUN" not in result.reason
        assert "made nothing worse" in result.reason

    def test_operator_summary_says_no_branch_was_judged(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """`format_batch` is the stdout half of the report, and a red composite
        with no explanation is how this bug stayed expensive."""
        batch = rv.revalidate_group(
            self._candidates(), _live_config(git_fleet),
            runner=self._baseline_red_runner,
        )
        rendered = "\n".join(rv.format_batch(batch))

        assert "RED BASELINE" in rendered
        assert "no branch was judged" in rendered
        assert "Fix the baseline" in rendered
        assert "SUITE FAILED" not in rendered
        assert "INFRASTRUCTURE FAILURE" not in rendered

    def test_a_red_baseline_never_narrows_to_per_entry_runs(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """N solo re-runs would hit the identical pre-existing failures.

        Narrowing would turn one red baseline into N branches that each look
        individually broken — the precise impression this exists to remove —
        and would pay N suite runs to learn nothing.
        """
        calls: list[str] = []

        def runner(command, cwd, timeout):
            calls.append(command)
            return self._baseline_red_runner(command, cwd, timeout)

        batch = rv.revalidate_group(
            self._candidates(), _live_config(git_fleet), runner=runner,
        )

        assert batch.ok is False
        assert batch.fell_back is False
        assert batch.per_entry == []
        assert batch.culprits == []
        assert len(calls) == 1

    def test_a_red_baseline_still_counts_as_one_suite_run(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """Unlike INFRA (0), the suite DID run here.

        `suite_runs` answers "how many suites did that just run", so counting 0
        would misreport the cost. The two outcomes agree that no verdict may be
        recorded and disagree about what it cost — both halves matter.
        """
        batch = rv.revalidate_group(
            self._candidates(), _live_config(git_fleet),
            runner=self._baseline_red_runner,
        )
        assert batch.suite_runs == 1

    def test_infra_wins_when_both_markers_are_present(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """A run that could not run says nothing about any baseline.

        If both markers somehow appear, INFRA is the safer of the two claims:
        it asserts less (no verdict, no cost) and does not imply that a
        comparison against the merge-base actually happened.
        """
        def runner(command, cwd, timeout):
            return _Run(
                rv.RUNNER_INFRA_EXIT,
                stdout="RESULT: INFRA (rust)\nRESULT: BASELINE-RED (python)\n",
            )

        result = rv.revalidate(
            self._candidates(), _live_config(git_fleet), runner=runner,
        )
        assert result.kind == rv.KIND_INFRA


class TestIsBaselineRedFailure:
    """Unit coverage for the classifier itself (#2170)."""

    @pytest.mark.parametrize(
        ("rc", "output"),
        [
            (rv.RUNNER_BASELINE_RED_EXIT, "RESULT: BASELINE-RED (python)"),
            (1, "RESULT: BASELINE-RED (python) — every failure reproduces"),
            (4, "FAIL(python): ...\nRESULT: BASELINE-RED (python)\ntrailing\n"),
        ],
    )
    def test_positive_signals(self, rc: int, output: str) -> None:
        assert rv.is_baseline_red_failure(rc, output) is True

    def test_the_runners_exit_code_alone_is_not_enough(self) -> None:
        """A repo's own test command may legitimately exit 4 — pytest itself
        uses 4 for a usage error. Keying on the number would excuse a genuinely
        red suite as "not the branch's fault", which is the direction that could
        eventually launder a merge, so the marker in the output is what decides.
        """
        assert rv.is_baseline_red_failure(rv.RUNNER_BASELINE_RED_EXIT, "boom") is False

    @pytest.mark.parametrize(
        ("rc", "output"),
        [
            (1, "RESULT: FAIL (python)"),
            (1, "FAILED tests/test_x.py::test_y - AssertionError"),
            (4, ""),
            # This repo's own pytest arm contains tests whose assertion text and
            # parametrize ids embed this marker verbatim (the class above, and
            # tests/test_coord_test_runner_baseline.py). If one of them ever
            # fails for an unrelated reason, pytest reproduces the marker in its
            # output — but never at the start of a line. A bare substring match
            # would excuse that genuine failure as a red baseline and hide it.
            (1, "E       assert 'RESULT: BASELINE-RED' in out"),
            (
                1,
                "FAILED tests/test_revalidate.py::test_x"
                "[RESULT: BASELINE-RED (python)] - AssertionError",
            ),
            (
                1,
                "      RESULT: BASELINE-RED (python) — indented, as the runner's"
                " own `tail -n 40 ... | sed 's/^/      /'` dump always is",
            ),
        ],
    )
    def test_negative_signals(self, rc: int, output: str) -> None:
        assert rv.is_baseline_red_failure(rc, output) is False

    def test_it_is_not_conflated_with_infrastructure(self) -> None:
        """The two classifiers must not overlap: one means "never ran", the
        other means "ran, and was already red"."""
        baseline = "RESULT: BASELINE-RED (python)"
        infra = "RESULT: INFRA (rust)"
        assert rv.is_baseline_red_failure(4, baseline) is True
        assert rv.is_infrastructure_failure(4, baseline) is False
        assert rv.is_infrastructure_failure(3, infra) is True
        assert rv.is_baseline_red_failure(3, infra) is False

    def test_baseline_red_is_not_narrowable(self) -> None:
        """Pinned as data, not just as behaviour: N solo re-runs would hit the
        same pre-existing failures."""
        assert rv.KIND_BASELINE_RED not in rv.NARROWABLE_KINDS

    def test_baseline_red_did_run_a_suite(self) -> None:
        """Deliberately NOT in `NO_SUITE_RAN_KINDS` — the suite ran (twice, in
        fact: once on the branch and once on the merge-base)."""
        assert rv.KIND_BASELINE_RED not in rv.NO_SUITE_RAN_KINDS
        assert rv.KIND_INFRA in rv.NO_SUITE_RAN_KINDS


# ══════════════════════════════════════════════════════════════════════════════
# 7. #2231: the conflict verdict is HANDED OFF, not just printed
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def git_fleet_conflicting(tmp_path: Path):
    """The quadraui #306/#309 shape: sibling branches appending to ONE file.

    `main` already carries issue 101's append (it merged first — the sibling
    that "won"), so:

    * `issue-102-w2` appends a different line at the same spot → genuine
      content conflict against the current base;
    * `issue-103-w3` touches its own file → composes cleanly, and is here so
      the batch fallback has an innocent branch to exonerate.

    Same wiring as :func:`git_fleet` otherwise (bare origin + local checkout).
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"

    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "shared.txt").write_text("base\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "base")

    for issue, aid in ((101, "w1"), (102, "w2")):
        _git(seed, "checkout", "-q", "-b", f"issue-{issue}-{aid}", "main")
        (seed / "shared.txt").write_text(f"base\nfrom {issue}\n")
        _git(seed, "add", ".")
        _git(seed, "commit", "-q", "-m", f"issue {issue}")

    _git(seed, "checkout", "-q", "-b", "issue-103-w3", "main")
    (seed / "f103.txt").write_text("issue 103\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "issue 103")

    # #101 merges first — this is the base move that stales every sibling
    # verdict AND the content change the survivors now conflict with.
    _git(seed, "checkout", "-q", "main")
    _git(seed, "merge", "-q", "--ff-only", "issue-101-w1")

    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(origin)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(checkout)],
        check=True, capture_output=True, text=True,
    )
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "t")
    return checkout


def _conflict_candidates(aids_issues):
    out = []
    for aid, issue in aids_issues:
        entry = _entry(aid, issue=issue)
        entry.target_branch_head_sha = "base-new"
        out.append(mq.RevalidationCandidate(
            entry=entry,
            work_assignment_id=aid,
            smoke=mq.SmokeVerdictStatus(
                ok=False, kind=mq.SMOKE_STALE, assignment_id=aid,
                anchor="base", recorded_sha="base-old", current_sha="base-new",
            ),
        ))
    return out


class TestComposeConflictIsAttributed:
    """`revalidate_group` already composed the branch and watched `git merge`
    fail. #2231: that verdict is now carried out as structured data (the
    candidate, not a display label) so a caller can act on it."""

    def test_single_candidate_conflict_is_attributed_to_it(
        self, git_fleet_conflicting: Path, coord_db,
    ) -> None:
        def runner(command, cwd, timeout):  # pragma: no cover — must not run
            raise AssertionError("the suite must never run for a branch that "
                                 "does not even compose")

        batch = rv.revalidate_group(
            _conflict_candidates([("w2", 102)]),
            _live_config(git_fleet_conflicting),
            runner=runner,
        )

        assert batch.composite.kind == rv.KIND_COMPOSE
        assert [c.work_assignment_id for c, _ in batch.conflicted] == ["w2"]
        assert batch.per_entry == [], "N=1 never falls back (#1715)"

    def test_batch_fallback_names_only_the_conflicting_branch(
        self, git_fleet_conflicting: Path, coord_db,
    ) -> None:
        """The innocent sibling passes alone and merges; only the branch that
        genuinely won't compose is handed to the conflict path."""
        recorded: list = []
        with patch(
            "coord.state.record_test_verdict",
            side_effect=lambda **kw: recorded.append(kw),
        ):
            batch = rv.revalidate_group(
                _conflict_candidates([("w2", 102), ("w3", 103)]),
                _live_config(git_fleet_conflicting),
                runner=lambda command, cwd, timeout: _Run(0),
            )

        assert batch.composite.kind == rv.KIND_COMPOSE
        assert [c.work_assignment_id for c, _ in batch.conflicted] == ["w2"]
        assert batch.recorded == ["w3"]
        assert [r["assignment_id"] for r in recorded] == ["w3"]

    def test_a_red_suite_is_not_a_conflict(
        self, git_fleet: Path, coord_db,
    ) -> None:
        """Only KIND_COMPOSE routes to the rebase worker. A branch that
        composes and then fails its tests is a verdict problem, and handing it
        to a conflict-fix would be nonsense."""
        batch = rv.revalidate_group(
            _conflict_candidates([("w1", 101)]),
            _live_config(git_fleet),
            runner=lambda command, cwd, timeout: _Run(1, stdout="E assert"),
        )

        assert batch.composite.kind == rv.KIND_SUITE
        assert batch.conflicted == []


class TestComposeConflictErrorWording:
    """The text is load-bearing, not cosmetic: two string-matching consumers
    read it and disagreeing with either one reproduces #2231."""

    def test_classifies_as_rebaseable(self) -> None:
        entry = _entry("w2", issue=102)
        assert mq.classify_conflict(rv.compose_conflict_error(entry)) == "rebaseable"

    def test_does_not_read_as_a_stale_smoke_reason(self) -> None:
        entry = _entry("w2", issue=102)
        error = rv.compose_conflict_error(entry)
        assert mq.is_stale_smoke_reason(error) is False
        # `coord drive` classifies off a superset of those markers — if it read
        # "smoke", #1738 would answer a conflict with a re-test round.
        from coord.drive import _merge_gate_kind
        assert _merge_gate_kind(error) is None

    def test_names_the_branch_and_its_base(self) -> None:
        error = rv.compose_conflict_error(_entry("w2", issue=102))
        assert "issue-102-w2" in error
        assert "origin/main" in error


class TestRevalidateConflictBlackBox:
    """#2231 acceptance: "seed two branches that both append to one file, merge
    the first, and assert the second is reported as conflicted and gets a
    conflict-fix dispatch — not a re-test"."""

    @staticmethod
    def _seed(git_fleet_conflicting: Path, tmp_path: Path, *, test_command: str):
        from coord.state import save_board

        cfg = tmp_path / "coordinator.yml"
        cfg.write_text(
            CONFIG_YAML.format(repo_path=str(git_fleet_conflicting)).replace(
                'test_command: "true"', f'test_command: "{test_command}"',
            )
        )
        entry = _entry("w2", issue=102)
        entry.branch_head_sha = "branch-sha-102"
        mq.save_queue([entry])
        save_board(Board(active=[], completed=[_tested_work("w2", issue=102)]))
        _stamp_anchors(
            "w2", head_sha="branch-sha-102", base_sha="base-old",
            patch_id="patch-102",
        )
        return cfg

    def test_conflicted_entry_is_dispatched_not_re_tested(
        self, git_fleet_conflicting: Path, tmp_path: Path, coord_db,
    ) -> None:
        from unittest.mock import MagicMock

        ran = tmp_path / "suite-ran"
        cfg = self._seed(
            git_fleet_conflicting, tmp_path, test_command=f"touch {ran}",
        )
        fake_fix = MagicMock()
        fake_fix.machine_name = "laptop"

        with patch(
            "coord.conflict_fix.dispatch_conflict_fix", return_value=fake_fix,
        ) as dcf, patch("coord.state.record_test_verdict") as record:
            result = _invoke(
                ["merge", "--config", str(cfg), "--revalidate"],
                git_fleet_conflicting,
            )

        assert result.exit_code == 0, result.output
        # 1. The diagnosis is acted on — this is the whole issue.
        assert dcf.called, "expected a conflict-fix dispatch for a branch that "
        assert "conflict-fix dispatched to laptop" in result.output
        # 2. ...and NOT as a re-test: no suite ran, no verdict was rewritten.
        assert not ran.exists(), "the suite must not run for a conflicted branch"
        record.assert_not_called()
        # 3. The entry reports the conflict, not (only) a stale verdict.
        assert _states() == {"w2": mq.CONFLICT}
        error = {x.assignment_id: x.error for x in mq.load_queue()}["w2"]
        assert mq.classify_conflict(error) == "rebaseable"
        assert mq.is_stale_smoke_reason(error) is False
        assert "does not compose" in result.output
        assert "not a stale verdict" in result.output

    def test_only_path_dispatches_too(
        self, git_fleet_conflicting: Path, tmp_path: Path, coord_db,
    ) -> None:
        """`coord drive` merges through `--only`; the surgical lane must reach
        the same handoff (the #1474 lesson, re-learned)."""
        from unittest.mock import MagicMock

        cfg = self._seed(git_fleet_conflicting, tmp_path, test_command="true")
        fake_fix = MagicMock()
        fake_fix.machine_name = "laptop"

        with patch(
            "coord.conflict_fix.dispatch_conflict_fix", return_value=fake_fix,
        ) as dcf:
            result = _invoke(
                ["merge", "--config", str(cfg), "--only", "w2", "--revalidate"],
                git_fleet_conflicting,
            )

        assert result.exit_code == 0, result.output
        assert dcf.called
        assert _states() == {"w2": mq.CONFLICT}

    def test_dry_run_dispatches_nothing(
        self, git_fleet_conflicting: Path, tmp_path: Path, coord_db,
    ) -> None:
        cfg = self._seed(git_fleet_conflicting, tmp_path, test_command="true")

        with patch("coord.conflict_fix.dispatch_conflict_fix") as dcf:
            result = _invoke(
                ["merge", "--config", str(cfg), "--revalidate", "--dry-run"],
                git_fleet_conflicting,
            )

        assert result.exit_code == 0, result.output
        dcf.assert_not_called()
        assert _states() == {"w2": mq.PENDING}

    def test_a_clean_branch_still_takes_the_re_test_path(
        self, blackbox,
    ) -> None:
        """Acceptance: "A genuinely stale verdict on a cleanly-composing branch
        still takes the #1738 path, unchanged." Here that means: revalidated,
        merged, and never routed to a conflict-fix."""
        cfg, checkout = blackbox

        with patch("coord.conflict_fix.dispatch_conflict_fix") as dcf:
            result = _invoke(["merge", "--config", str(cfg), "--revalidate"], checkout)

        assert result.exit_code == 0, result.output
        dcf.assert_not_called()
        assert _states() == {"w1": mq.MERGED, "w2": mq.MERGED}


# ══════════════════════════════════════════════════════════════════════════════
# 7. #2829: `_auto_revalidate_tick` — the unattended sibling of `--revalidate`
# ══════════════════════════════════════════════════════════════════════════════

class TestAutoRevalidateTick:
    """``coord.serve_app._auto_revalidate_tick`` — the daemon tick behind
    ``merge.auto_revalidate``. Reuses the ``blackbox`` fixture (real git
    fleet, two genuinely stale entries) for the happy-path tests so the
    composite the tick runs is the REAL suite, not a stub -- the same
    discipline :class:`TestMergeRevalidateBlackBox` uses for the CLI path.
    """

    @staticmethod
    def _auto_config(cfg_path: Path, *, max_batch: int = 3):
        from coord.config import load as load_config

        config = load_config(cfg_path)
        config.merge.auto_revalidate = True
        config.merge.auto_revalidate_max_batch = max_batch
        return config

    def test_no_eligible_candidates_is_a_no_op(
        self, git_fleet: Path, tmp_path: Path, coord_db,
    ) -> None:
        from coord.serve_app import _auto_revalidate_tick

        cfg_path = tmp_path / "coordinator.yml"
        cfg_path.write_text(CONFIG_YAML.format(repo_path=str(git_fleet)))
        config = self._auto_config(cfg_path)

        assert _auto_revalidate_tick(config) == []

    def test_merges_both_stale_entries_after_one_composite_run(
        self, blackbox,
    ) -> None:
        """The headline case: a lock-free composite passes, the base-SHA
        recheck confirms nothing moved, and both entries merge -- exactly
        what an operator's ``coord merge --revalidate`` would have done by
        hand."""
        from coord.serve_app import _auto_revalidate_tick

        cfg_path, checkout = blackbox
        config = self._auto_config(cfg_path)

        stack = _gh_patches(checkout)
        for p in stack:
            p.start()
        try:
            events = _auto_revalidate_tick(config)
        finally:
            for p in reversed(stack):
                p.stop()

        merged = {ev.entry.assignment_id for ev in events if ev.kind == "merged"}
        assert merged == {"w1", "w2"}, [
            (ev.kind, ev.entry.assignment_id) for ev in events
        ]
        assert _states() == {"w1": mq.MERGED, "w2": mq.MERGED}

    def test_discards_the_result_when_the_base_moves_before_the_recheck(
        self, blackbox, monkeypatch,
    ) -> None:
        """The correctness crux (#2829): simulates a concurrent merge moving
        the base while the (lock-free) composite is running, by making the
        recheck report "moved" regardless. Nothing may merge on that
        result -- the composite validated a tree that is no longer the
        base, and merging it anyway is exactly the soundness bug the recheck
        exists to prevent."""
        from coord import revalidate as rv
        from coord.serve_app import _auto_revalidate_tick

        cfg_path, checkout = blackbox
        config = self._auto_config(cfg_path)

        monkeypatch.setattr(
            rv, "revalidated_base_still_current", lambda *a, **kw: False,
        )

        stack = _gh_patches(checkout)
        for p in stack:
            p.start()
        try:
            events = _auto_revalidate_tick(config)
        finally:
            for p in reversed(stack):
                p.stop()

        assert events == []
        # The composite still ran for real (that work wasn't wasted) --
        # what must NOT have happened is a merge off its result.
        assert _states() == {"w1": mq.PENDING, "w2": mq.PENDING}

    def test_processes_only_one_group_per_tick(
        self, git_fleet: Path, tmp_path: Path, coord_db, monkeypatch,
    ) -> None:
        """Per-tick ceiling (#2829): even with two eligible ``(repo,
        target_branch)`` groups, at most ONE composite runs per call --
        never a burst."""
        from coord import merge_queue as mq2
        from coord import revalidate as rv
        from coord.serve_app import _auto_revalidate_tick

        cfg_path = tmp_path / "coordinator.yml"
        cfg_path.write_text(CONFIG_YAML.format(repo_path=str(git_fleet)))
        config = self._auto_config(cfg_path)

        e1 = _entry("w1", issue=101, target="main")
        e2 = _entry("w2", issue=102, target="develop")
        cand1 = mq2.RevalidationCandidate(
            entry=e1, work_assignment_id="w1",
            smoke=mq2.SmokeVerdictStatus(ok=False, kind=mq2.SMOKE_STALE, assignment_id="w1"),
        )
        cand2 = mq2.RevalidationCandidate(
            entry=e2, work_assignment_id="w2",
            smoke=mq2.SmokeVerdictStatus(ok=False, kind=mq2.SMOKE_STALE, assignment_id="w2"),
        )
        monkeypatch.setattr(mq2, "load_queue", lambda: [e1, e2])
        monkeypatch.setattr(
            mq2, "revalidation_candidates", lambda *a, **kw: [cand1, cand2],
        )

        calls: list[list] = []

        def fake_group(group, cfg, echo=None):
            calls.append(list(group))
            return rv.BatchRevalidationResult(
                composite=rv.RevalidationResult(
                    ok=False, kind=rv.KIND_SUITE, reason="boom",
                ),
            )

        monkeypatch.setattr(rv, "revalidate_group", fake_group)

        _auto_revalidate_tick(config)

        assert len(calls) == 1, (
            f"expected exactly one composite call per tick, got {len(calls)}"
        )
        assert len(calls[0]) == 1, "each group here has exactly one candidate"

    def test_caps_batch_size_to_auto_revalidate_max_batch(
        self, git_fleet: Path, tmp_path: Path, coord_db, monkeypatch,
    ) -> None:
        """Batch ceiling (#2829): ``auto_revalidate_max_batch`` caps how many
        candidates ONE group's composite may cover, well under
        ``coord.revalidate.MAX_REVALIDATION_BATCH``."""
        from coord import merge_queue as mq2
        from coord import revalidate as rv
        from coord.serve_app import _auto_revalidate_tick

        cfg_path = tmp_path / "coordinator.yml"
        cfg_path.write_text(CONFIG_YAML.format(repo_path=str(git_fleet)))
        config = self._auto_config(cfg_path, max_batch=1)

        e1 = _entry("w1", issue=101, target="main")
        e2 = _entry("w2", issue=102, target="main")
        cand1 = mq2.RevalidationCandidate(
            entry=e1, work_assignment_id="w1",
            smoke=mq2.SmokeVerdictStatus(ok=False, kind=mq2.SMOKE_STALE, assignment_id="w1"),
        )
        cand2 = mq2.RevalidationCandidate(
            entry=e2, work_assignment_id="w2",
            smoke=mq2.SmokeVerdictStatus(ok=False, kind=mq2.SMOKE_STALE, assignment_id="w2"),
        )
        monkeypatch.setattr(mq2, "load_queue", lambda: [e1, e2])
        monkeypatch.setattr(
            mq2, "revalidation_candidates", lambda *a, **kw: [cand1, cand2],
        )

        calls: list[list] = []

        def fake_group(group, cfg, echo=None):
            calls.append(list(group))
            return rv.BatchRevalidationResult(
                composite=rv.RevalidationResult(
                    ok=False, kind=rv.KIND_SUITE, reason="boom",
                ),
            )

        monkeypatch.setattr(rv, "revalidate_group", fake_group)

        _auto_revalidate_tick(config)

        assert len(calls) == 1
        assert len(calls[0]) == 1, (
            "auto_revalidate_max_batch=1 must cap the composite to one "
            "candidate even though two were eligible"
        )

    # The lock-ordering test (composite runs unlocked, merge step blocks on
    # `_merge_lock`) needs a cross-thread-safe DB connection, which is
    # `rw_db` in tests/test_serve.py (mirroring production's
    # `check_same_thread=False`) -- the autouse `coord_db` fixture used here
    # is thread-bound `:memory:` and cannot be touched from a background
    # thread. See test_serve.py::test_auto_revalidate_composite_runs_with_merge_lock_released.
