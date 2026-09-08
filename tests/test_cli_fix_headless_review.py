"""#1622: `coord fix <REVIEW_ID>` — the headless same-branch fix door.

Black-box tests over the CLI.  The capability itself (same-branch dispatch)
already existed in ``coord.auto_loop``; what these cover is the *door* — that a
headless invocation reaches it, that it lands on the ORIGINAL branch, and that
every guard the auto-loop path applies still applies when a human (or a drive)
types the command instead of a review transition firing it.

The companion trigger — red CI on the work row's PR — is covered at the bottom.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord import state as state_mod
from coord.cli import main
from coord.models import Assignment, Board


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
pipeline:
  auto_loop: true
  max_review_iterations: 3
ci_store:
  type: none
"""

CONFIG_YAML_LOOP_DISABLED = CONFIG_YAML.replace(
    "  auto_loop: true", "  auto_loop: false"
)

# #3208: a second machine capable of the same repo, for the reachability-
# fallback / --machine redirect tests below.
CONFIG_YAML_TWO_MACHINES = CONFIG_YAML.replace(
    "pipeline:",
    "  - name: server\n"
    "    host: server.tailnet\n"
    "    repos: [api]\n"
    "    repo_paths:\n"
    "      api: /tmp/api-server\n"
    "pipeline:",
)

WORK_BRANCH = "issue-42-feature-x"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def config_file_two_machines(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML_TWO_MACHINES)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coord_db) -> Path:
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state_mod, "COORD_DIR", d)
    return d


def _work(**overrides) -> Assignment:
    defaults = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="Add feature X",
        briefing="original briefing",
        assignment_id="work-abc",
        status="done",
        branch=WORK_BRANCH,
        pr_url="https://github.com/acme/api/pull/7",
        dispatched_at=0.0,
        finished_at=1.0,
        type="work",
        review_state="dispatched",
        review_iteration=0,
    )
    defaults.update(overrides)
    return Assignment(**defaults)


def _review(**overrides) -> Assignment:
    defaults = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="[review] Add feature X",
        assignment_id="review-xyz",
        status="done",
        branch=WORK_BRANCH,
        dispatched_at=1.0,
        finished_at=2.0,
        type="review",
        review_of_assignment_id="work-abc",
    )
    defaults.update(overrides)
    return Assignment(**defaults)


def _seed(work: Assignment, review: Assignment, *, verdict: str = "request-changes",
          body: str = "## Blocking\n- 1. Missing test for the empty-input case\n") -> None:
    """Put the pair on the board and cache the reviewer's findings in the DB.

    The DB cache is source #1 of ``_load_review_findings``, so seeding it is
    what a real `coord notify` / `report-result --body-file` would have done.
    """
    state_mod.save_board(Board(completed=[work, review]))
    state_mod.update_assignment_review_findings(
        review.assignment_id, verdict=verdict, body=body
    )


def _http_mock(new_id: str = "fix-new") -> MagicMock:
    mock = MagicMock()
    mock.post.return_value.json.return_value = {"id": new_id}
    mock.post.return_value.raise_for_status = MagicMock()
    return mock


def _run(config_file: Path, *args: str):
    return CliRunner().invoke(main, ["fix", *args, "--config", str(config_file)])


@pytest.fixture(autouse=True)
def _machine_always_reachable(monkeypatch: pytest.MonkeyPatch):
    """#3208: `coord fix` now probes reachability (`coord.dispatch.
    select_fix_machine`, via `coord.network.fetch_status`) before picking a
    machine for either door — the REVIEW-id door (`_dispatch_fix`) and the
    WORK-id door (`_dispatch_followup`). Every test in this module predates
    that probe and configures a single ``laptop.tailnet`` host that doesn't
    actually resolve — default it (and anything else) to reachable so these
    tests keep exercising what they said they exercise. The unreachable-
    machine behavior itself is covered where it's introduced (auto_loop /
    dispatch tests), not duplicated here."""
    from coord.network import StatusResult

    monkeypatch.setattr(
        "coord.network.fetch_status",
        lambda machine, timeout=3.0: StatusResult(data={}),
    )


# ── The acceptance case ──────────────────────────────────────────────────────


class TestHeadlessFixFromReview:
    def test_dispatches_on_the_original_branch_and_bumps_the_iteration(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Seed work + request-changes review → `coord fix <review id>`.

        Asserts the whole point of #1622: branch identity is preserved (no new
        ``issue-N-*`` branch, no orphan PR) and ``review_iteration`` moved 0→1.
        """
        _seed(_work(), _review())
        http = _http_mock()

        with patch("coord.auto_loop.httpx", http), \
             patch("coord.auto_loop.record_dispatched_assignment"), \
             patch("coord.github_ops.post_issue_comment"):
            result = _run(config_file, "review-xyz")

        assert result.exit_code == 0, result.output
        assert "fix-new" in result.output

        # The dispatch payload pins the worker to the reviewed work's branch.
        payload = http.post.call_args.kwargs["json"]
        assert payload["target_branch"] == WORK_BRANCH
        assert payload["branch"] == "main"  # base to cut from if absent

        board = state_mod.load_board()
        fixes = [a for a in board.active if a.assignment_id == "fix-new"]
        assert len(fixes) == 1, board.active
        fix_row = fixes[0]
        assert fix_row.branch == WORK_BRANCH
        assert fix_row.review_iteration == 1
        assert fix_row.review_of_assignment_id == "work-abc"
        assert fix_row.pr_url == "https://github.com/acme/api/pull/7"
        # No second branch was invented anywhere on the board.
        assert {a.branch for a in board.active if a.branch} == {WORK_BRANCH}

    def test_reviewer_findings_reach_the_fix_briefing(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed(_work(), _review(), body="## Blocking\n- 1. The retry loop is unbounded\n")
        http = _http_mock()

        with patch("coord.auto_loop.httpx", http), \
             patch("coord.auto_loop.record_dispatched_assignment"), \
             patch("coord.github_ops.post_issue_comment"):
            result = _run(config_file, "review-xyz")

        assert result.exit_code == 0, result.output
        assert "The retry loop is unbounded" in http.post.call_args.kwargs["json"]["briefing"]

    def test_verdict_is_propagated_onto_the_work_row(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#1663's bookkeeping half runs on this path too, not just the drain."""
        _seed(_work(), _review())
        http = _http_mock()

        with patch("coord.auto_loop.httpx", http), \
             patch("coord.auto_loop.record_dispatched_assignment"), \
             patch("coord.github_ops.post_issue_comment"):
            assert _run(config_file, "review-xyz").exit_code == 0

        board = state_mod.load_board()
        work = board.find_by_id("work-abc")
        assert work.review_verdict == "request-changes"
        assert work.review_state == "done"


# ── The guards, still holding ────────────────────────────────────────────────


class TestGuardsSurviveTheCliDoor:
    def test_max_review_iterations_caps_the_loop_headlessly(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """At the cap, a headless dispatch is refused — not silently allowed."""
        _seed(_work(review_iteration=3), _review())  # max_review_iterations: 3
        http = _http_mock()

        with patch("coord.auto_loop.httpx", http), \
             patch("coord.auto_loop.record_dispatched_assignment"), \
             patch("coord.github_ops.post_issue_comment"):
            result = _run(config_file, "review-xyz")

        assert result.exit_code != 0
        assert "max_review_iterations" in result.output
        http.post.assert_not_called()
        assert state_mod.load_board().active == []

    def test_interactive_work_is_not_followed_by_a_headless_fix(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#555: a claude-pty work completion owns its branch."""
        _seed(_work(provider_name="claude-pty"), _review())
        http = _http_mock()

        with patch("coord.auto_loop.httpx", http), \
             patch("coord.auto_loop.record_dispatched_assignment"), \
             patch("coord.github_ops.post_issue_comment"):
            result = _run(config_file, "review-xyz")

        assert result.exit_code != 0
        assert "#555" in result.output
        assert "claude-pty" in result.output
        http.post.assert_not_called()
        assert state_mod.load_board().active == []

    def test_force_overrides_only_the_interactive_exclusion(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed(_work(provider_name="claude-pty"), _review())
        http = _http_mock()

        with patch("coord.auto_loop.httpx", http), \
             patch("coord.auto_loop.record_dispatched_assignment"), \
             patch("coord.github_ops.post_issue_comment"):
            result = _run(config_file, "review-xyz", "--force")

        assert result.exit_code == 0, result.output
        assert http.post.call_args.kwargs["json"]["target_branch"] == WORK_BRANCH

    def test_force_does_not_override_the_iteration_cap(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--force is scoped to #555; the cap is not negotiable from the CLI."""
        _seed(_work(review_iteration=3, provider_name="claude-pty"), _review())
        http = _http_mock()

        with patch("coord.auto_loop.httpx", http), \
             patch("coord.auto_loop.record_dispatched_assignment"), \
             patch("coord.github_ops.post_issue_comment"):
            result = _run(config_file, "review-xyz", "--force")

        assert result.exit_code != 0
        assert "max_review_iterations" in result.output
        http.post.assert_not_called()

    def test_terminal_work_is_refused(
        self, config_file: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """#522: merged/closed work must not re-enter the review→fix loop."""
        _seed(_work(), _review())
        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
        http = _http_mock()

        with patch("coord.auto_loop.httpx", http), \
             patch("coord.auto_loop.record_dispatched_assignment"), \
             patch("coord.github_ops.post_issue_comment"):
            result = _run(config_file, "review-xyz")

        assert result.exit_code != 0
        assert "merged/closed" in result.output
        http.post.assert_not_called()

    def test_approve_verdict_has_nothing_to_fix(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed(_work(), _review(), verdict="approve", body="LGTM.")
        http = _http_mock()

        with patch("coord.auto_loop.httpx", http), \
             patch("coord.auto_loop.record_dispatched_assignment"), \
             patch("coord.github_ops.post_issue_comment"):
            result = _run(config_file, "review-xyz")

        assert result.exit_code != 0
        assert "nothing to fix" in result.output
        http.post.assert_not_called()

    def test_advisory_only_request_changes_does_not_dispatch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#476/#1456: an explicitly-empty blocking section is approve-with-nits."""
        _seed(
            _work(), _review(),
            body=(
                "## Blocking issues\n\nNone.\n\n"
                "## Nits\n- 1. Rename `tmp` to `scratch`\n"
            ),
        )
        http = _http_mock()

        with patch("coord.auto_loop.httpx", http), \
             patch("coord.auto_loop.record_dispatched_assignment"), \
             patch("coord.github_ops.post_issue_comment"):
            result = _run(config_file, "review-xyz")

        assert result.exit_code != 0
        assert "no blocking findings" in result.output
        http.post.assert_not_called()

    def test_auto_loop_disabled_is_reported_not_ignored(
        self, tmp_path: Path, coord_dir: Path
    ) -> None:
        cfg = tmp_path / "off.yml"
        cfg.write_text(CONFIG_YAML_LOOP_DISABLED)
        _seed(_work(), _review())
        http = _http_mock()

        with patch("coord.auto_loop.httpx", http), \
             patch("coord.auto_loop.record_dispatched_assignment"):
            result = _run(cfg, "review-xyz")

        assert result.exit_code != 0
        assert "auto_loop" in result.output
        http.post.assert_not_called()

    def test_review_without_linked_work_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        review = _review(review_of_assignment_id="gone-999")
        state_mod.save_board(Board(completed=[review]))
        state_mod.update_assignment_review_findings(
            "review-xyz", verdict="request-changes", body="fix it"
        )

        result = _run(config_file, "review-xyz")
        assert result.exit_code != 0
        assert "no linked work assignment" in result.output

    def test_no_findings_anywhere_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        state_mod.save_board(Board(completed=[_work(), _review()]))  # no findings cached
        http = _http_mock()

        with patch("coord.auto_loop.httpx", http), \
             patch("coord.auto_loop.parse_review_from_agent", lambda *a, **k: None), \
             patch("coord.review.fetch_review_findings_from_github", lambda *a, **k: None):
            result = _run(config_file, "review-xyz")

        assert result.exit_code != 0
        assert "no structured review findings" in result.output
        http.post.assert_not_called()

    def test_guidance_on_a_review_id_is_pinned_above_the_findings(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#2092: the reviewer's findings are the briefing, but the operator's
        guidance is the only source of maintainer decisions the reviewer
        cannot have — it must reach the worker, not vanish behind a warning.
        Routed through the #603 pinned per-issue context store, it lands
        ABOVE the reviewer's findings in the fix briefing."""
        _seed(_work(), _review(), body="## Blocking\n- 1. GET /outbox is undocumented\n")
        http = _http_mock()

        with patch("coord.auto_loop.httpx", http), \
             patch("coord.auto_loop.record_dispatched_assignment"), \
             patch("coord.github_ops.post_issue_comment"):
            result = _run(
                config_file, "review-xyz",
                "--guidance", "MAINTAINER DECISION: keep GET /outbox.",
            )

        assert result.exit_code == 0, result.output
        assert "pinned to issue #42 context" in result.output

        briefing = http.post.call_args.kwargs["json"]["briefing"]
        assert "MAINTAINER DECISION: keep GET /outbox." in briefing
        assert briefing.index("MAINTAINER DECISION") < briefing.index(
            "GET /outbox is undocumented"
        )

        # It also outlives this one dispatch — it's a durable per-issue entry.
        from coord.state import issue_context_block

        assert "MAINTAINER DECISION: keep GET /outbox." in issue_context_block("api", 42)


# ── Part 3: red CI as the third trigger ──────────────────────────────────────


CONFIG_YAML_CI = CONFIG_YAML.replace("  type: none", "  type: github")


def _check(name: str, conclusion: str):
    from coord.ci_store import CheckRun

    return CheckRun(
        name=name, status="completed", conclusion=conclusion,
        url=f"https://github.com/acme/api/runs/{name}",
        run_id=name, started_at=None, completed_at=None,
    )


class TestFixFromRedCi:
    @pytest.fixture
    def ci_config(self, tmp_path: Path) -> Path:
        p = tmp_path / "ci.yml"
        p.write_text(CONFIG_YAML_CI)
        return p

    def test_red_ci_on_a_passing_work_row_dispatches_a_same_branch_fix(
        self, ci_config: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """vimcode #613's shape: local Test passed, CI is red, and the fix has
        to land on the branch CI is gating."""
        work = _work(test_state="passed", smoke_test="pass")
        state_mod.save_board(Board(completed=[work]))

        fake_store = MagicMock()
        fake_store.is_available = True
        fake_store.list_checks_for_pr.return_value = [
            _check("build", "success"),
            _check("test (3.12)", "failure"),
        ]
        monkeypatch.setattr(
            "coord.ci_store.build_ci_store", lambda _type, **_kw: fake_store
        )

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            captured["target_branch"] = proposal.target_branch
            return {"id": "fix-ci"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["fix", "work-abc", "--config", str(ci_config)]
            )

        assert result.exit_code == 0, result.output
        assert captured["target_branch"] == WORK_BRANCH
        assert "test (3.12)" in captured["briefing"]
        assert "CI failure" in captured["briefing"]
        fake_store.list_checks_for_pr.assert_called_once_with("acme/api", 7)

    def test_green_ci_still_refuses(
        self, ci_config: Path, coord_dir: Path, monkeypatch
    ) -> None:
        work = _work(test_state="passed", smoke_test="pass")
        state_mod.save_board(Board(completed=[work]))

        fake_store = MagicMock()
        fake_store.is_available = True
        fake_store.list_checks_for_pr.return_value = [_check("build", "success")]
        monkeypatch.setattr(
            "coord.ci_store.build_ci_store", lambda _type, **_kw: fake_store
        )

        result = CliRunner().invoke(
            main, ["fix", "work-abc", "--config", str(ci_config)]
        )
        assert result.exit_code != 0
        assert "expected a failed test verdict" in result.output
        assert "--force" in result.output

    def test_green_ci_with_force_but_no_guidance_still_refuses(
        self, ci_config: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """#2051: `--force` alone would dispatch a fix worker with nothing to
        go on — no failed verdict, no CI read, no guidance. Require the
        caller to say what's actually broken."""
        work = _work(test_state="passed", smoke_test="pass")
        state_mod.save_board(Board(completed=[work]))

        fake_store = MagicMock()
        fake_store.is_available = True
        fake_store.list_checks_for_pr.return_value = [_check("build", "success")]
        monkeypatch.setattr(
            "coord.ci_store.build_ci_store", lambda _type, **_kw: fake_store
        )

        result = CliRunner().invoke(
            main, ["fix", "work-abc", "--force", "--config", str(ci_config)]
        )
        assert result.exit_code != 0
        assert "--guidance" in result.output

    def test_green_ci_with_force_and_guidance_dispatches(
        self, ci_config: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """#2051: a caller who knows the PR is red — and the automated CI
        read missed it — has a way through via `--force` + `--guidance`."""
        work = _work(test_state="passed", smoke_test="pass")
        state_mod.save_board(Board(completed=[work]))

        fake_store = MagicMock()
        fake_store.is_available = True
        fake_store.list_checks_for_pr.return_value = [_check("build", "success")]
        monkeypatch.setattr(
            "coord.ci_store.build_ci_store", lambda _type, **_kw: fake_store
        )

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            captured["target_branch"] = proposal.target_branch
            return {"id": "fix-forced"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "fix", "work-abc", "--force",
                    "--guidance", "windows job is red, see run 123",
                    "--config", str(ci_config),
                ],
            )

        assert result.exit_code == 0, result.output
        assert captured["target_branch"] == WORK_BRANCH
        assert "windows job is red, see run 123" in captured["briefing"]

    def test_a_failed_local_test_takes_priority_and_reads_no_ci(
        self, ci_config: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """The cheap in-DB path stays zero-I/O — no `gh pr checks` shell-out."""
        work = _work(test_state="failed", test_reason="assert 1 == 2")
        state_mod.save_board(Board(completed=[work]))

        def boom(*a, **k):
            raise AssertionError("CI must not be consulted when the test gate failed")

        monkeypatch.setattr("coord.ci_store.build_ci_store", boom)

        with patch("coord.dispatch.dispatch", return_value={"id": "fix-t"}), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["fix", "work-abc", "--config", str(ci_config)]
            )

        assert result.exit_code == 0, result.output

    def test_dispatch_refused_by_a_pre_dispatch_guard_exits_distinctly(
        self, ci_config: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """#1844: THIS `coord fix` arm (a work assignment id — a failed local
        test or red CI, drive.py's `command=("fix", state.work_aid)`) goes
        through `_dispatch_followup` → `dispatch()`, so a deterministic
        pre-dispatch guard refusal (`coord.dispatch.DispatchRefused`) must
        exit `EXIT_DISPATCH_REFUSED`, not the generic 1, so `coord drive`'s
        subprocess call can tell it apart from a transient failure instead
        of retrying it. (The review-id arm routes through
        `coord.auto_loop._dispatch_fix` instead, which never runs this
        guard — not covered by this test.)
        """
        from coord.dispatch import DispatchRefused
        from coord.drive import EXIT_DISPATCH_REFUSED

        work = _work(test_state="failed", test_reason="assert 1 == 2")
        state_mod.save_board(Board(completed=[work]))

        def boom(*a, **k):
            raise AssertionError("CI must not be consulted when the test gate failed")

        monkeypatch.setattr("coord.ci_store.build_ci_store", boom)

        with patch(
            "coord.dispatch.dispatch",
            side_effect=DispatchRefused("no acceptance slice yet — run ..."),
        ):
            result = CliRunner().invoke(
                main, ["fix", "work-abc", "--config", str(ci_config)]
            )

        assert result.exit_code == EXIT_DISPATCH_REFUSED
        assert result.exit_code != 1
        assert "no acceptance slice yet" in result.output


# ── Part 4 (#2344): a failed oracle-loop acceptance trust gate as a fourth
# trigger, mirroring the red-CI door above ────────────────────────────────────


class TestFixFromFailedAcceptanceGate:
    def test_failed_trust_gate_on_an_otherwise_green_row_dispatches_a_fix(
        self, config_file: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """`_decide_acceptance_gate` (coord/drive.py) dispatches `coord fix
        <work_aid>` the moment the trust gate fails, even when Test has
        already passed and CI is green (ms-65 / #2282's shape). Before
        #2344 this refused every time with "expected a failed test
        verdict" — there was no door for it."""
        work = _work(test_state="passed", smoke_test="pass")
        state_mod.save_board(Board(completed=[work]))
        state_mod.record_acceptance_verdict(
            assignment_id=work.assignment_id,
            acceptance_state="failed",
            acceptance_reason="acceptance suite: 2/7 passed — see run log",
        )

        def boom(*a, **k):
            raise AssertionError(
                "CI must not be consulted when the trust gate already failed"
            )

        monkeypatch.setattr("coord.ci_store.build_ci_store", boom)

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            captured["target_branch"] = proposal.target_branch
            return {"id": "fix-acceptance"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["fix", "work-abc", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert captured["target_branch"] == WORK_BRANCH
        assert "Acceptance trust-gate failure" in captured["briefing"]
        assert "acceptance suite: 2/7 passed" in captured["briefing"]

    def test_passing_trust_gate_does_not_open_the_door(
        self, config_file: Path, coord_dir: Path, monkeypatch
    ) -> None:
        work = _work(test_state="passed", smoke_test="pass")
        state_mod.save_board(Board(completed=[work]))
        state_mod.record_acceptance_verdict(
            assignment_id=work.assignment_id, acceptance_state="passed"
        )

        monkeypatch.setattr(
            "coord.ci_store.build_ci_store",
            lambda _type, **_kw: MagicMock(is_available=False),
        )

        result = CliRunner().invoke(
            main, ["fix", "work-abc", "--config", str(config_file)]
        )
        assert result.exit_code != 0
        assert "expected a failed test" in result.output
        assert "acceptance_state is 'passed'" in result.output

    def test_a_failed_local_test_takes_priority_over_the_trust_gate(
        self, config_file: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """When both Test and the trust gate are red, the existing Test
        failure story keeps priority — it's what a caller is used to
        seeing and is at least as informative."""
        work = _work(test_state="failed", test_reason="assert 1 == 2")
        state_mod.save_board(Board(completed=[work]))
        state_mod.record_acceptance_verdict(
            assignment_id=work.assignment_id,
            acceptance_state="failed",
            acceptance_reason="acceptance suite: 2/7 passed",
        )

        def boom(*a, **k):
            raise AssertionError("CI must not be consulted when the test gate failed")

        monkeypatch.setattr("coord.ci_store.build_ci_store", boom)

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            return {"id": "fix-t"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["fix", "work-abc", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert "Test failure" in captured["briefing"]
        assert "assert 1 == 2" in captured["briefing"]


# ── #3208: a failed UAT verdict defaults --guidance instead of demanding it ──


class TestFixFromUatFailure:
    def test_uat_failure_with_force_defaults_guidance_from_uat_reason(
        self, config_file: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """The board already holds a full write-up of what's wrong
        (`uat_reason`, from `coord uat --failed` or a customer portal
        sign-off) — a caller forcing a fix for a UAT failure should not
        have to retype it via --guidance."""
        work = _work(
            test_state="passed", smoke_test="pass",
            uat_state="failed",
            uat_reason="Gate-A divergence: empty-cart checkout still shows the old total",
        )
        state_mod.save_board(Board(completed=[work]))

        monkeypatch.setattr(
            "coord.ci_store.build_ci_store",
            lambda _type, **_kw: MagicMock(is_available=False),
        )

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            return {"id": "fix-uat"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["fix", "work-abc", "--force", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert "defaulted to uat_reason" in result.output
        assert "UAT failure" in captured["briefing"]
        assert "empty-cart checkout still shows the old total" in captured["briefing"]

    def test_uat_failure_still_needs_force(
        self, config_file: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """A UAT failure is a fifth door onto `coord fix`, but — unlike a
        failed test verdict, red CI, or a failed acceptance trust gate — it
        does not by itself skip the --force gate: nothing here has
        independently confirmed the PR is red the way those three do."""
        work = _work(
            test_state="passed", smoke_test="pass",
            uat_state="failed", uat_reason="checkout total is wrong",
        )
        state_mod.save_board(Board(completed=[work]))

        monkeypatch.setattr(
            "coord.ci_store.build_ci_store",
            lambda _type, **_kw: MagicMock(is_available=False),
        )

        result = CliRunner().invoke(
            main, ["fix", "work-abc", "--config", str(config_file)]
        )
        assert result.exit_code != 0
        assert "expected a failed test" in result.output

    def test_explicit_guidance_overrides_the_uat_reason_default(
        self, config_file: Path, coord_dir: Path, monkeypatch
    ) -> None:
        work = _work(
            test_state="passed", smoke_test="pass",
            uat_state="failed", uat_reason="checkout total is wrong",
        )
        state_mod.save_board(Board(completed=[work]))

        monkeypatch.setattr(
            "coord.ci_store.build_ci_store",
            lambda _type, **_kw: MagicMock(is_available=False),
        )

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            return {"id": "fix-uat-2"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, [
                    "fix", "work-abc", "--force",
                    "--guidance", "Ignore the total; fix the currency symbol instead.",
                    "--config", str(config_file),
                ]
            )

        assert result.exit_code == 0, result.output
        assert "defaulted to uat_reason" not in result.output
        assert "Ignore the total; fix the currency symbol instead." in captured["briefing"]


# ── #3210: a same-branch fix invalidates the ORIGINAL's stale UAT verdict ────


class TestFixInvalidatesStaleUatVerdict:
    def test_dangerous_direction_a_stale_pass_is_cleared_by_a_test_failure_fix(
        self, config_file: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """The risk #3210 actually cares about: UAT already passed, then the
        Test gate went red and a same-branch fix is dispatched to address
        THAT. The stale `uat_state=passed` describes code this fix is about
        to rewrite — it must not survive the dispatch, or the merge gate
        would clear a rewrite nobody has looked at."""
        work = _work(
            test_state="failed", test_reason="assert 1 == 2",
            uat_state="passed", uat_actor="operator",
        )
        state_mod.save_board(Board(completed=[work]))

        monkeypatch.setattr(
            "coord.ci_store.build_ci_store",
            lambda _type, **_kw: MagicMock(is_available=False),
        )

        with patch("coord.dispatch.dispatch", return_value={"id": "fix-clears-uat"}), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["fix", "work-abc", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert "uat verdict cleared" in result.output

        from coord.board_service import read_board

        reloaded = read_board().find_by_id("work-abc")
        assert reloaded is not None
        assert reloaded.uat_state is None
        assert reloaded.uat_actor is None

    def test_observed_direction_a_stale_fail_is_cleared_by_the_uat_triggered_fix(
        self, config_file: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """format-converter#6's shape: UAT itself failed, `coord fix --force`
        dispatches a same-branch fix off that failure, and the OLD failed
        verdict — the one that just got addressed — must not keep blocking
        the merge gate afterward."""
        work = _work(
            test_state="passed", smoke_test="pass",
            uat_state="failed",
            uat_reason="renderLoadedScreen shows binary garbage instead of the source doc",
        )
        state_mod.save_board(Board(completed=[work]))

        monkeypatch.setattr(
            "coord.ci_store.build_ci_store",
            lambda _type, **_kw: MagicMock(is_available=False),
        )

        with patch("coord.dispatch.dispatch", return_value={"id": "fix-clears-uat-2"}), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["fix", "work-abc", "--force", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert "uat verdict cleared" in result.output

        from coord.board_service import read_board

        reloaded = read_board().find_by_id("work-abc")
        assert reloaded is not None
        assert reloaded.uat_state is None

    def test_no_prior_uat_verdict_prints_nothing_extra(
        self, config_file: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """No verdict to invalidate — the new #3210 line must not appear."""
        work = _work(test_state="failed", test_reason="assert 1 == 2")
        state_mod.save_board(Board(completed=[work]))

        monkeypatch.setattr(
            "coord.ci_store.build_ci_store",
            lambda _type, **_kw: MagicMock(is_available=False),
        )

        with patch("coord.dispatch.dispatch", return_value={"id": "fix-no-uat"}), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["fix", "work-abc", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert "uat verdict cleared" not in result.output


# ── #3208: redirect a same-branch fix around an unreachable machine ─────────


class TestFixMachineRedirect:
    """The WORK-id door onto `coord fix` (failed test / red CI / acceptance
    / UAT) must not blindly re-aim at the original worker's machine when
    it's unreachable — it falls back to another capable, reachable one
    automatically, or refuses by NAME with a `--machine` hint (#3208)."""

    def test_falls_back_automatically_when_original_is_unreachable(
        self, config_file_two_machines: Path, coord_dir: Path, monkeypatch
    ) -> None:
        from coord.network import StatusResult

        work = _work(smoke_test="fail", smoke_test_reason="flaky test")
        state_mod.save_board(Board(completed=[work]))

        def _status(machine, timeout=3.0):
            if machine.name == "laptop":
                return StatusResult(error="timeout")
            return StatusResult(data={})

        monkeypatch.setattr("coord.network.fetch_status", _status)
        monkeypatch.setattr(
            "coord.github_ops.branch_exists_on_remote", lambda *a, **k: True,
        )

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["machine_name"] = proposal.machine_name
            return {"id": "fix-redirect"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["fix", "work-abc", "--config", str(config_file_two_machines)]
            )

        assert result.exit_code == 0, result.output
        assert "redirecting" in result.output
        assert "laptop" in result.output
        assert "server" in result.output
        assert captured["machine_name"] == "server"

    def test_machine_flag_redirects_explicitly(
        self, config_file_two_machines: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """--machine pins the dispatch to exactly the named machine, even
        when the original is reachable — the caller is asserting where to
        send it."""
        work = _work(smoke_test="fail", smoke_test_reason="flaky test")
        state_mod.save_board(Board(completed=[work]))

        monkeypatch.setattr(
            "coord.github_ops.branch_exists_on_remote", lambda *a, **k: True,
        )

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["machine_name"] = proposal.machine_name
            return {"id": "fix-explicit"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, [
                    "fix", "work-abc", "--machine", "server",
                    "--config", str(config_file_two_machines),
                ]
            )

        assert result.exit_code == 0, result.output
        assert captured["machine_name"] == "server"

    def test_refusal_names_the_unreachable_machine_when_nothing_is_usable(
        self, config_file_two_machines: Path, coord_dir: Path, monkeypatch
    ) -> None:
        from coord.network import StatusResult

        work = _work(smoke_test="fail", smoke_test_reason="flaky test")
        state_mod.save_board(Board(completed=[work]))

        monkeypatch.setattr(
            "coord.network.fetch_status",
            lambda machine, timeout=3.0: StatusResult(error="timeout"),
        )

        result = CliRunner().invoke(
            main, ["fix", "work-abc", "--config", str(config_file_two_machines)]
        )

        assert result.exit_code != 0
        assert "laptop" in result.output
        assert "server" in result.output
        assert "--machine" in result.output
        # The dispatch-level "timed out" message this issue was filed about
        # must no longer be the ONLY thing on screen — the machine names
        # and the redirect hint must be there too.
        assert "cannot dispatch fix" in result.output
