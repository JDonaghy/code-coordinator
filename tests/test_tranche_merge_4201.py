"""Tests for the native tranche row's coord-side merge, integration-surface
regen, and LLM integrator fallback (#4129 Phase 1, this slice #4201):

- `coord.merge_queue.merge_tranche_children` — merges every `"done"` child
  onto `row.base_sha`, regenerates the repo's declared
  `integration_surfaces` ONCE across the result, and reports whether a
  conflict (or a failing regen/check) outside those surfaces remains.
- `coord.dispatch.dispatch_tranche_integrator` / `parse_tranche_integrator_
  result` / `build_tranche_integrator_proposal` — the single LLM fallback
  leg's wire contract and dispatch.
- `coord.drive.integrate_tranche_row` — the orchestration glue between the
  two, folding the merge verdict into `row.state`.

#4199 (row shape) and #4200 (parallel child dispatch, the result contract)
are the sibling slices this builds on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coord.config import Config
from coord.dispatch import (
    TRANCHE_INTEGRATOR_RESULT_MARKER,
    build_tranche_integrator_briefing,
    build_tranche_integrator_proposal,
    dispatch_tranche_integrator,
    parse_tranche_integrator_result,
)
from coord.drive import integrate_tranche_row, tranche_row_all_children_terminal
from coord.merge_queue import (
    TrancheMergeResult,
    merge_tranche_children,
    tranche_integration_branch_name,
)
from coord.models import IntegrationSurface, Machine, Repo, TrancheChild, TrancheRow


# ── real-git fixtures, mirroring tests/test_conflict_fix.py's own
# TestAttemptMechanicalSurfaceResolution fleet-building helpers ────────────


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "t@t.com"], path)
    _git(["config", "user.name", "Test"], path)


def _write_surface_scripts(path: Path) -> None:
    (path / "regen.py").write_text(
        "data = open('source.txt').read()\n"
        "open('derived.json', 'w').write('COUNT=' + str(len(data)) + chr(10))\n"
    )
    (path / "check.py").write_text(
        "import sys\n"
        "data = open('source.txt').read()\n"
        "expected = 'COUNT=' + str(len(data)) + chr(10)\n"
        "actual = open('derived.json').read()\n"
        "sys.exit(0 if actual == expected else 1)\n"
    )


def _regen(path: Path) -> None:
    data = (path / "source.txt").read_text()
    (path / "derived.json").write_text(f"COUNT={len(data)}\n")


def _build_tranche_fleet(
    tmp_path: Path, *, row_id: int = 1, uncovered_conflict: bool = False,
    surface_conflict: bool = False,
) -> tuple[Path, str]:
    """Bare ``origin`` plus a local checkout, with a base commit and two
    child branches (``tranche-{row_id}-child-101``/``-102``) editing
    ``source.txt`` and independently regenerating ``derived.json``.
    Returns ``(checkout, base_sha)``.

    By default (no flag set) child 101 and 102 edit DIFFERENT lines of
    ``source.txt`` with edits of the SAME length, so ``source.txt`` merges
    cleanly on both sides and the two children's ``derived.json`` outputs
    (a byte count) land on the identical value too — no conflict anywhere,
    just a stale-but-matching artifact.

    ``uncovered_conflict=True`` makes child 102 edit the SAME line of
    ``source.txt`` child 101 already touched, so the conflict spills
    outside the one declared surface path.

    ``surface_conflict=True`` makes child 102 edit a DIFFERENT line of
    ``source.txt`` (so ``source.txt`` itself still merges cleanly) but with
    an edit of a DIFFERENT length than child 101's — so the two children's
    ``derived.json`` (``COUNT=<len>``) disagree on the same line and git
    reports a genuine conflict confined entirely to the declared
    ``derived.json`` surface.
    """
    seed = tmp_path / "seed"
    origin = tmp_path / "origin.git"
    checkout = tmp_path / "checkout"

    _init_repo(seed)
    (seed / "source.txt").write_text("alpha\nbeta\ngamma\n")
    _write_surface_scripts(seed)
    _regen(seed)
    _git(["add", "."], seed)
    _git(["commit", "-qm", "base"], seed)
    base_sha = _git(["rev-parse", "HEAD"], seed).stdout.strip()

    _git(["checkout", "-qb", f"tranche-{row_id}-child-101"], seed)
    (seed / "source.txt").write_text("alpha-A\nbeta\ngamma\n")
    _regen(seed)
    _git(["add", "."], seed)
    _git(["commit", "-qm", "child 101"], seed)

    _git(["checkout", "-q", "main"], seed)
    _git(["checkout", "-qb", f"tranche-{row_id}-child-102"], seed)
    if uncovered_conflict:
        (seed / "source.txt").write_text("alpha-B\nbeta\ngamma\n")
    elif surface_conflict:
        (seed / "source.txt").write_text("alpha\nbeta\ngamma-BB\n")
    else:
        (seed / "source.txt").write_text("alpha\nbeta\ngamma-B\n")
    _regen(seed)
    _git(["add", "."], seed)
    _git(["commit", "-qm", "child 102"], seed)

    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(origin)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(checkout)],
        check=True, capture_output=True, text=True,
    )
    _git(["config", "user.email", "t@t.com"], checkout)
    _git(["config", "user.name", "Test"], checkout)
    return checkout, base_sha


def _tranche_config(checkout: Path, *, surfaces: list) -> Config:
    return Config(
        repos=[
            Repo(name="api", github="acme/api", integration_surfaces=surfaces),
        ],
        machines=[
            Machine(
                name="local", host="local.tail", repos=["api"],
                repo_paths={"api": str(checkout)},
            ),
        ],
    )


def _surfaces() -> list:
    return [
        IntegrationSurface(
            paths=["derived.json"], regen="python3 regen.py", check="python3 check.py",
        ),
    ]


def _row(base_sha: str, *, row_id: int = 1, child_states=("done", "done")) -> TrancheRow:
    return TrancheRow(
        id=row_id, repo_name="api", issue_number=1, issue_title="Decompose the big thing",
        base_sha=base_sha, state="dispatched",
        children=[
            TrancheChild(issue_number=101, repo_name="api", title="Child A", state=child_states[0]),
            TrancheChild(issue_number=102, repo_name="api", title="Child B", state=child_states[1]),
        ],
    )


class TestTrancheIntegrationBranchName:
    def test_deterministic_from_row_id(self) -> None:
        row = _row("deadbeef", row_id=7)
        assert tranche_integration_branch_name(row) == "tranche-7-integrated"

    def test_raises_when_row_has_no_id(self) -> None:
        row = _row("deadbeef")
        row.id = None
        with pytest.raises(ValueError, match="row.id is None"):
            tranche_integration_branch_name(row)


class TestMergeTrancheChildren:
    @pytest.fixture(autouse=True)
    def _isolated_coord_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("coord.state.COORD_DIR", tmp_path / "coord-state")

    def test_merges_every_done_child_and_regenerates_surface_once(
        self, tmp_path: Path,
    ) -> None:
        checkout, base_sha = _build_tranche_fleet(tmp_path)
        config = _tranche_config(checkout, surfaces=_surfaces())
        row = _row(base_sha)

        result = merge_tranche_children(row, config)

        assert result.ok, result.detail
        assert not result.needs_integrator
        assert result.unresolved_conflicts == ()
        assert result.integrated_sha

        _git(["fetch", "origin"], checkout)
        remote_sha = _git(
            ["rev-parse", "origin/tranche-1-integrated"], checkout,
        ).stdout.strip()
        assert remote_sha == result.integrated_sha
        source = _git(
            ["show", f"{remote_sha}:source.txt"], checkout,
        ).stdout
        assert source == "alpha-A\nbeta\ngamma-B\n"
        derived = _git(["show", f"{remote_sha}:derived.json"], checkout).stdout
        assert derived == f"COUNT={len(source)}\n"

    def test_reports_needs_integrator_on_conflict_outside_surfaces(
        self, tmp_path: Path,
    ) -> None:
        checkout, base_sha = _build_tranche_fleet(tmp_path, uncovered_conflict=True)
        config = _tranche_config(checkout, surfaces=_surfaces())
        row = _row(base_sha)

        result = merge_tranche_children(row, config)

        assert not result.ok
        assert result.needs_integrator
        assert result.unresolved_conflicts == ("source.txt",)
        assert result.integrated_sha is None

        # Partial progress (child 101 merged cleanly before 102 conflicted)
        # is still pushed so an integrator leg has somewhere to resume from.
        _git(["fetch", "origin"], checkout)
        remote = _git(
            ["ls-remote", "origin", "refs/heads/tranche-1-integrated"], checkout,
        ).stdout
        assert remote.strip(), "partial progress must be pushed for the integrator"

        # No leftover in-progress merge state in the base checkout.
        status = _git(["status", "--porcelain"], checkout).stdout
        assert status == ""

    def test_resolves_surface_only_conflict_via_ours_and_regen(
        self, tmp_path: Path,
    ) -> None:
        """A conflict confined to a declared `integration_surfaces` path
        (`derived.json`) is resolved mechanically via `git checkout --ours`
        + stage + placeholder commit, then the declared surface is
        regenerated once across the merged tree — so the pushed result
        both merges successfully AND ends up with a correct, non-stale
        `derived.json` (not just whatever the placeholder commit left)."""
        checkout, base_sha = _build_tranche_fleet(tmp_path, surface_conflict=True)
        config = _tranche_config(checkout, surfaces=_surfaces())
        row = _row(base_sha)

        result = merge_tranche_children(row, config)

        assert result.ok, result.detail
        assert not result.needs_integrator
        assert result.unresolved_conflicts == ()
        assert result.integrated_sha

        _git(["fetch", "origin"], checkout)
        remote_sha = _git(
            ["rev-parse", "origin/tranche-1-integrated"], checkout,
        ).stdout.strip()
        assert remote_sha == result.integrated_sha
        source = _git(
            ["show", f"{remote_sha}:source.txt"], checkout,
        ).stdout
        assert source == "alpha-A\nbeta\ngamma-BB\n"
        derived = _git(["show", f"{remote_sha}:derived.json"], checkout).stdout
        assert derived == f"COUNT={len(source)}\n"

    def test_dirty_check_failure_after_regen_falls_back_to_pre_regen_push(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed `git status --porcelain` observation after regen must
        decline (push the pre-regen `merged_sha`, report `needs_integrator`)
        rather than be read as "tree is clean" and pushed as `ok=True`
        (#2096: a failed observation is not a permissive default)."""
        from coord import merge_queue as merge_queue_module

        real_run = merge_queue_module._tranche_merge_run

        def _flaky_run(args, **kwargs):
            if args[:3] == ["git", "status", "--porcelain"]:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="simulated git status failure",
                )
            return real_run(args, **kwargs)

        monkeypatch.setattr(merge_queue_module, "_tranche_merge_run", _flaky_run)

        checkout, base_sha = _build_tranche_fleet(tmp_path)
        config = _tranche_config(checkout, surfaces=_surfaces())
        row = _row(base_sha)

        result = merge_tranche_children(row, config)

        assert not result.ok
        assert result.needs_integrator
        assert result.integrated_sha is None
        assert "could not observe working-tree state" in result.detail

        _git(["fetch", "origin"], checkout)
        remote = _git(
            ["ls-remote", "origin", "refs/heads/tranche-1-integrated"], checkout,
        ).stdout
        assert remote.strip(), "partial (pre-regen) progress must still be pushed"

    def test_regen_check_failure_falls_back_to_pre_regen_push(
        self, tmp_path: Path,
    ) -> None:
        checkout, base_sha = _build_tranche_fleet(tmp_path)
        config = _tranche_config(checkout, surfaces=[
            IntegrationSurface(
                paths=["derived.json"], regen="python3 regen.py",
                check="python3 -c 'import sys; sys.exit(1)'",
            ),
        ])
        row = _row(base_sha)

        result = merge_tranche_children(row, config)

        assert not result.ok
        assert result.needs_integrator
        assert result.unresolved_conflicts == ()
        assert "check command failed" in result.detail

        _git(["fetch", "origin"], checkout)
        remote = _git(
            ["ls-remote", "origin", "refs/heads/tranche-1-integrated"], checkout,
        ).stdout
        assert remote.strip()

    def test_declines_without_pushing_when_a_child_failed(
        self, tmp_path: Path,
    ) -> None:
        checkout, base_sha = _build_tranche_fleet(tmp_path)
        config = _tranche_config(checkout, surfaces=_surfaces())
        row = _row(base_sha, child_states=("done", "failed"))

        result = merge_tranche_children(row, config)

        assert not result.ok
        assert not result.needs_integrator
        assert "failed" in result.detail
        remote = _git(
            ["ls-remote", "origin", "refs/heads/tranche-1-integrated"], checkout,
        ).stdout
        assert remote.strip() == ""

    def test_declines_when_not_every_child_is_terminal(self, tmp_path: Path) -> None:
        checkout, base_sha = _build_tranche_fleet(tmp_path)
        config = _tranche_config(checkout, surfaces=_surfaces())
        row = _row(base_sha, child_states=("done", "dispatched"))

        result = merge_tranche_children(row, config)

        assert not result.ok
        assert not result.needs_integrator

    def test_declines_when_config_is_none(self, tmp_path: Path) -> None:
        row = _row("deadbeef")
        result = merge_tranche_children(row, None)
        assert not result.ok
        assert not result.needs_integrator
        assert "config" in result.detail

    def test_declines_when_no_local_checkout(self) -> None:
        config = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="local", host="local.tail", repos=["api"])],
        )
        row = _row("deadbeef")
        result = merge_tranche_children(row, config)
        assert not result.ok
        assert not result.needs_integrator
        assert "no local repo checkout" in result.detail


class TestParseTrancheIntegratorResult:
    def test_valid_marker_parses(self) -> None:
        text = (
            f'{TRANCHE_INTEGRATOR_RESULT_MARKER} {{"outcome": "done", '
            '"summary": "merged the rest", "files": ["source.txt"]}'
        )
        result = parse_tranche_integrator_result(text)
        assert result == {
            "outcome": "done",
            "summary": "merged the rest",
            "files": ["source.txt"],
            "fail_closed_unparseable": False,
        }

    @pytest.mark.parametrize(
        "text",
        [None, "", "no marker here", f"{TRANCHE_INTEGRATOR_RESULT_MARKER} not json"],
    )
    def test_fails_closed_on_unparseable_input(self, text) -> None:
        result = parse_tranche_integrator_result(text)
        assert result["outcome"] == "failed"
        assert result["fail_closed_unparseable"] is True


class TestBuildTrancheIntegratorBriefingAndProposal:
    def test_briefing_lists_children_and_merge_detail(self) -> None:
        row = _row("deadbeef")
        merge_result = TrancheMergeResult(
            False, True, None, ("source.txt",), "conflicts outside surfaces: source.txt",
        )
        briefing = build_tranche_integrator_briefing(row, merge_result)
        assert "tranche-1-child-101" in briefing
        assert "tranche-1-child-102" in briefing
        assert "tranche-1-integrated" in briefing
        assert "conflicts outside surfaces" in briefing
        assert TRANCHE_INTEGRATOR_RESULT_MARKER in briefing

    def test_proposal_targets_the_integration_branch(self) -> None:
        row = _row("deadbeef")
        merge_result = TrancheMergeResult(False, True, None, (), "regen failed")
        proposal = build_tranche_integrator_proposal(row, merge_result, "laptop")
        assert proposal.machine_name == "laptop"
        assert proposal.repo_name == "api"
        assert proposal.type == "work"
        assert proposal.target_branch == "tranche-1-integrated"


class TestDispatchTrancheIntegrator:
    def test_dispatches_and_confirms_assignment_id(self) -> None:
        row = _row("deadbeef")
        merge_result = TrancheMergeResult(False, True, None, ("source.txt",), "conflict")
        dispatcher = MagicMock(return_value={"id": "agent-assignment-9"})

        result = dispatch_tranche_integrator(
            row, merge_result,
            Config(repos=[Repo(name="api", github="acme/api")], machines=[]),
            "laptop", dispatcher=dispatcher,
        )

        assert result == {
            "ok": True, "assignment_id": "agent-assignment-9", "machine_name": "laptop",
        }

    def test_fails_closed_when_agent_response_reports_failure(self) -> None:
        row = _row("deadbeef")
        merge_result = TrancheMergeResult(False, True, None, ("source.txt",), "conflict")
        dispatcher = MagicMock(return_value={
            "id": "agent-assignment-9", "status": "failed", "error": "disk full",
        })

        result = dispatch_tranche_integrator(
            row, merge_result,
            Config(repos=[Repo(name="api", github="acme/api")], machines=[]),
            "laptop", dispatcher=dispatcher,
        )

        assert result["ok"] is False
        assert "disk full" in result["error"]

    def test_fails_closed_when_dispatcher_raises(self) -> None:
        row = _row("deadbeef")
        merge_result = TrancheMergeResult(False, True, None, ("source.txt",), "conflict")
        dispatcher = MagicMock(side_effect=RuntimeError("agent unreachable"))

        result = dispatch_tranche_integrator(
            row, merge_result,
            Config(repos=[Repo(name="api", github="acme/api")], machines=[]),
            "laptop", dispatcher=dispatcher,
        )

        assert result["ok"] is False
        assert "agent unreachable" in result["error"]


class TestIntegrateTrancheRow:
    def test_successful_merge_marks_row_done(self) -> None:
        row = _row("deadbeef")
        merger = MagicMock(return_value=TrancheMergeResult(True, False, "newsha", (), "ok"))
        integrator_dispatcher = MagicMock()

        result_row, merge_result, integrator_dispatch = integrate_tranche_row(
            row, Config(repos=[], machines=[]), ["laptop"],
            merger=merger, integrator_dispatcher=integrator_dispatcher,
        )

        assert result_row is row
        assert row.state == "done"
        assert merge_result.ok
        assert integrator_dispatch is None
        integrator_dispatcher.assert_not_called()

    def test_needs_integrator_and_dispatch_succeeds_keeps_row_integrating(self) -> None:
        row = _row("deadbeef")
        merge_result = TrancheMergeResult(False, True, None, ("source.txt",), "conflict")
        merger = MagicMock(return_value=merge_result)
        integrator_dispatcher = MagicMock(return_value={"ok": True, "assignment_id": "x"})

        result_row, returned_result, integrator_dispatch = integrate_tranche_row(
            row, Config(repos=[], machines=[]), ["laptop"],
            merger=merger, integrator_dispatcher=integrator_dispatcher,
        )

        assert row.state == "integrating"
        assert returned_result is merge_result
        # The dispatch handle (assignment id, machine) must survive for a
        # later collection slice — never silently discarded (#4200 review).
        assert integrator_dispatch == {"ok": True, "assignment_id": "x"}
        integrator_dispatcher.assert_called_once()
        assert integrator_dispatcher.call_args.args[0] is row
        assert integrator_dispatcher.call_args.args[1] is merge_result
        assert integrator_dispatcher.call_args.args[3] == "laptop"

    def test_needs_integrator_but_dispatch_fails_marks_row_failed(self) -> None:
        row = _row("deadbeef")
        merge_result = TrancheMergeResult(False, True, None, ("source.txt",), "conflict")
        merger = MagicMock(return_value=merge_result)
        integrator_dispatcher = MagicMock(return_value={"ok": False, "error": "no machine"})

        integrate_tranche_row(
            row, Config(repos=[], machines=[]), ["laptop"],
            merger=merger, integrator_dispatcher=integrator_dispatcher,
        )

        assert row.state == "failed"

    def test_unrecoverable_merge_failure_marks_row_failed_without_dispatch(self) -> None:
        row = _row("deadbeef", child_states=("done", "failed"))
        merge_result = TrancheMergeResult(False, False, None, (), "child failed")
        merger = MagicMock(return_value=merge_result)
        integrator_dispatcher = MagicMock()

        integrate_tranche_row(
            row, Config(repos=[], machines=[]), ["laptop"],
            merger=merger, integrator_dispatcher=integrator_dispatcher,
        )

        assert row.state == "failed"
        integrator_dispatcher.assert_not_called()

    def test_raises_when_not_every_child_is_terminal(self) -> None:
        row = _row("deadbeef", child_states=("done", "dispatched"))
        assert not tranche_row_all_children_terminal(row)
        with pytest.raises(ValueError, match="terminal state"):
            integrate_tranche_row(row, Config(repos=[], machines=[]), ["laptop"], merger=MagicMock())

    def test_raises_when_row_is_not_dispatched(self) -> None:
        row = _row("deadbeef")
        row.state = "integrating"
        with pytest.raises(ValueError, match="not 'dispatched'"):
            integrate_tranche_row(row, Config(repos=[], machines=[]), ["laptop"], merger=MagicMock())

    def test_raises_when_integrator_needed_but_no_machines(self) -> None:
        row = _row("deadbeef")
        merge_result = TrancheMergeResult(False, True, None, ("source.txt",), "conflict")
        merger = MagicMock(return_value=merge_result)
        with pytest.raises(ValueError, match="machine_names is empty"):
            integrate_tranche_row(row, Config(repos=[], machines=[]), [], merger=merger)
