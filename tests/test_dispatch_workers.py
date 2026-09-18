"""#3322: the human-attended fix doors share the fix-round counter.

`coord fix --fix-of` (``_dispatch_fix_of``) and `coord rework --rework-of`
(``_dispatch_rework_of``) both write to an existing ``issue-N-*`` branch, so
they write into the same ``review_iteration`` chain the headless auto-loop
bounce and the stalled-pipeline sweep write into. Each used to compute the
round number off ONE row of its own choosing — `--fix-of`'s resolved work row,
or (worse, for `--rework-of <branch>`) the *first* completed row found on the
branch, which is the oldest rather than the newest. Either way the counter
could repeat a number it had already spent, which silently stalled model
escalation and made ``pipeline.max_review_iterations`` unreachable.

These drive the real functions with ``dry_run=True``, which prints the round
number it resolved and then returns before launching anything.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from coord.commands.dispatch_workers import _dispatch_fix_of, _dispatch_rework_of
from coord.config import Config, ModelsConfig, PipelineConfig
from coord.models import Assignment, Board, Machine, Repo

BRANCH = "issue-7-widget"


@pytest.fixture
def repo() -> Repo:
    return Repo(name="api", github="acme/api", default_branch="main")


@pytest.fixture
def machine(repo: Repo) -> Machine:
    return Machine(
        name="laptop",
        host="laptop.tail",
        capabilities=["python"],
        repos=["api"],
        repo_paths={"api": "/work/api"},
    )


@pytest.fixture
def cfg(repo: Repo, machine: Machine) -> Config:
    return Config(
        repos=[repo],
        machines=[machine],
        models=ModelsConfig(default="sonnet", escalation=["haiku", "sonnet", "opus"]),
        pipeline=PipelineConfig(max_review_iterations=3, escalate_fix_model=True),
    )


def _work_row(
    assignment_id: str,
    review_iteration: int,
    *,
    branch: str | None = BRANCH,
    type: str = "work",  # noqa: A002 - matches Assignment's field name
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=7,
        issue_title="Widget is broken",
        assignment_id=assignment_id,
        status="done",
        branch=branch,
        pr_url="https://github.com/acme/api/pull/7",
        dispatched_at=float(review_iteration),
        finished_at=float(review_iteration) + 0.5,
        type=type,
        review_state="done",
        review_iteration=review_iteration,
    )


def _review_row(assignment_id: str, review_of: str) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=7,
        issue_title="[review] Widget is broken",
        assignment_id=assignment_id,
        status="done",
        branch=BRANCH,
        dispatched_at=9.0,
        finished_at=10.0,
        type="review",
        review_of_assignment_id=review_of,
        review_verdict="request-changes",
    )


def _chain(n_fix_rows: int) -> tuple[Assignment, Assignment, Board]:
    """Original work row (iter 0) + *n_fix_rows* fix rows (iter 1..N), plus a
    request-changes review pointing at the ORIGINAL row."""
    work = _work_row("work-orig", 0)
    review = _review_row("review-1", "work-orig")
    board = Board(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        active=[],
        completed=[work, review],
    )
    for i in range(1, n_fix_rows + 1):
        board.completed.append(_work_row(f"fix-{i}", i))
    return work, review, board


def _fake_provider() -> MagicMock:
    provider = MagicMock()
    provider.build_command.return_value = ["claude", "-p"]
    return provider


def _fake_machine_obj() -> MagicMock:
    machine_obj = MagicMock()
    machine_obj.repo_path.return_value = "/work/api"
    machine_obj.host = "laptop.tail"
    return machine_obj


def _run_fix(board: Board, cfg: Config, repo: Repo, *, force: bool = False) -> None:
    with patch(
        "coord.auto_loop._load_review_findings", return_value=None
    ):
        _dispatch_fix_of(
            machine="laptop",
            repo="api",
            issue=7,
            briefing="",
            model=None,
            dry_run=True,
            force=force,
            fix_of="review-1",
            cfg=cfg,
            machine_obj=_fake_machine_obj(),
            repo_cfg=repo,
            issue_title="Widget is broken",
            provider=_fake_provider(),
            _is_local=True,
            _svc=None,
            _interactive_board=lambda _builder: board,
            _issue_ctx="",
            _ctx_write_hint="",
        )


def _run_rework(
    board: Board, cfg: Config, repo: Repo, *, rework_of: str,
) -> None:
    _dispatch_rework_of(
        machine="laptop",
        repo="api",
        issue=7,
        briefing="Redo it.",
        model=None,
        dry_run=True,
        force=False,
        rework_of=rework_of,
        cfg=cfg,
        machine_obj=_fake_machine_obj(),
        repo_cfg=repo,
        issue_title="Widget is broken",
        provider=_fake_provider(),
        _is_local=True,
        _svc=None,
        _interactive_board=lambda _builder: board,
        _issue_ctx="",
    )


class TestFixOfIterationIsMonotonic:
    """`coord fix --fix-of` must produce a round strictly greater than the max
    already spent on that (repo, issue, branch)."""

    @pytest.mark.parametrize("existing_fix_rows", [0, 1, 2])
    def test_round_number_exceeds_every_round_already_on_the_branch(
        self, cfg: Config, repo: Repo, capsys, existing_fix_rows: int
    ) -> None:
        cfg.pipeline.max_review_iterations = 10
        _work, _review, board = _chain(existing_fix_rows)
        prior_max = max(
            (a.review_iteration or 0)
            for a in board.completed
            if a.type == "work" and a.branch == BRANCH
        )

        _run_fix(board, cfg, repo)

        out = capsys.readouterr().out
        expected = prior_max + 1
        assert f"(iteration {expected}/10)" in out, out
        assert expected > prior_max

    def test_does_not_reuse_a_round_when_fix_of_resolves_the_original_row(
        self, cfg: Config, repo: Repo, capsys
    ) -> None:
        """The regression itself: `--fix-of` resolves `work` through
        `review.review_of_assignment_id`, which points at the ORIGINAL
        iteration-0 row even after two headless fix rounds already ran."""
        cfg.pipeline.max_review_iterations = 10
        _work, _review, board = _chain(2)

        _run_fix(board, cfg, repo)

        out = capsys.readouterr().out
        assert "(iteration 3/10)" in out, out
        assert "(iteration 1/" not in out

    def test_escalated_model_climbs_with_the_round(
        self, cfg: Config, repo: Repo, capsys
    ) -> None:
        """Round 3 must be on the escalated tier, not back on models.default."""
        cfg.pipeline.max_review_iterations = 10
        _work, _review, board = _chain(2)

        _run_fix(board, cfg, repo)

        out = capsys.readouterr().out
        assert "model: opus" in out, out

    def test_cap_fires_once_the_chain_has_spent_every_round(
        self, cfg: Config, repo: Repo, capsys
    ) -> None:
        """With max_review_iterations=3 and fix rows at 1, 2 and 3 already on
        the branch, a further `coord fix` must refuse instead of recomputing a
        number that still looks under the cap."""
        cfg.pipeline.max_review_iterations = 3
        _work, _review, board = _chain(3)

        with pytest.raises(SystemExit) as excinfo:
            _run_fix(board, cfg, repo)

        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "max_review_iterations (3) reached" in err

    def test_force_overrides_the_cap(
        self, cfg: Config, repo: Repo, capsys
    ) -> None:
        cfg.pipeline.max_review_iterations = 3
        _work, _review, board = _chain(3)

        _run_fix(board, cfg, repo, force=True)

        captured = capsys.readouterr()
        assert "dispatching iteration 4 anyway (--force)" in captured.err
        assert "(iteration 4/3)" in captured.out

    def test_last_permitted_round_still_dispatches(
        self, cfg: Config, repo: Repo, capsys
    ) -> None:
        cfg.pipeline.max_review_iterations = 3
        _work, _review, board = _chain(2)

        _run_fix(board, cfg, repo)

        assert "(iteration 3/3)" in capsys.readouterr().out


class TestFixOfTitleDoesNotStack:
    """#3323: `_dispatch_fix_of` (`coord fix --fix-of`, the human-attended
    front door) must build the round-N title through the same
    `coord.auto_loop.fix_round_title` helper the headless bounce uses, so an
    already-marked incoming title collapses instead of stacking another
    `[fix-N]` in front of it — both in the dispatched spec and in the board
    record `coord fix` keeps."""

    def _run_fix_capturing_provider(
        self, board: Board, cfg: Config, repo: Repo, *, issue_title: str,
    ) -> MagicMock:
        provider = _fake_provider()
        with patch("coord.auto_loop._load_review_findings", return_value=None):
            _dispatch_fix_of(
                machine="laptop",
                repo="api",
                issue=7,
                briefing="",
                model=None,
                dry_run=True,
                force=False,
                fix_of="review-1",
                cfg=cfg,
                machine_obj=_fake_machine_obj(),
                repo_cfg=repo,
                issue_title=issue_title,
                provider=provider,
                _is_local=True,
                _svc=None,
                _interactive_board=lambda _builder: board,
                _issue_ctx="",
                _ctx_write_hint="",
            )
        return provider

    def test_round_3_title_has_no_residue_of_rounds_1_and_2(
        self, cfg: Config, repo: Repo,
    ) -> None:
        cfg.pipeline.max_review_iterations = 10
        _work, _review, board = _chain(2)

        provider = self._run_fix_capturing_provider(
            board, cfg, repo,
            issue_title="[fix-2] [fix-1] Widget is broken",
        )

        spec = provider.build_command.call_args.args[0]
        assert spec.issue_title == "[fix-3] Widget is broken"

    def test_a_clean_title_just_gets_prepended(
        self, cfg: Config, repo: Repo,
    ) -> None:
        cfg.pipeline.max_review_iterations = 10
        _work, _review, board = _chain(0)

        provider = self._run_fix_capturing_provider(
            board, cfg, repo, issue_title="Widget is broken",
        )

        spec = provider.build_command.call_args.args[0]
        assert spec.issue_title == "[fix-1] Widget is broken"


class TestReworkIterationIsMonotonic:
    """`coord rework` writes to the same branch, so it shares the counter."""

    def test_rework_of_assignment_id_reads_the_whole_chain(
        self, cfg: Config, repo: Repo, capsys
    ) -> None:
        cfg.pipeline.max_review_iterations = 10
        _work, _review, board = _chain(2)

        _run_rework(board, cfg, repo, rework_of="work-orig")

        assert "(iteration 3/10)" in capsys.readouterr().out

    def test_rework_of_branch_name_uses_the_newest_round_not_the_oldest(
        self, cfg: Config, repo: Repo, capsys
    ) -> None:
        """The branch-name fallback used to take the FIRST completed work row
        on the branch — the ORIGINAL iteration-0 row — and add one, so a
        third rework re-issued round 1."""
        cfg.pipeline.max_review_iterations = 10
        _work, _review, board = _chain(2)

        _run_rework(board, cfg, repo, rework_of=BRANCH)

        out = capsys.readouterr().out
        assert "(iteration 3/10)" in out, out

    def test_rework_on_a_branch_with_no_history_starts_at_one(
        self, cfg: Config, repo: Repo, capsys
    ) -> None:
        board = Board(repos=[Repo(name="api", github="acme/api")])

        _run_rework(board, cfg, repo, rework_of="issue-7-nothing-here")

        assert "(iteration 1/3)" in capsys.readouterr().out
