"""#2091: the Test gate must measure what CI measures, and the live-CI
fallback must not report "no red CI" when it never looked.

Two defects from the same episode (coord-portal #14):

1. The Test stage recorded ``test_passed`` in 1m51s on a commit whose CI run
   failed deterministically after 44m57s, because the suite the gate ran was a
   strict subset of the suite CI runs. ``repos[].ci_command`` is the missing
   declaration, and ``resolve_smoke_command`` is where it takes effect.
2. ``coord fix`` refused the follow-up dispatch citing "no red CI on its PR"
   while ``gh pr checks 42`` reported a failing check. Six distinct
   short-circuits in the CI read all returned a bare ``None``, making
   "nobody looked" indistinguishable from "CI is green".
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord import state as state_mod
from coord.cli import main
from coord.config import ConfigError
from coord.config import load as load_config
from coord.models import Assignment, Board, Repo
from coord.smoke import (
    SmokeCommand,
    build_smoke_briefing,
    resolve_smoke_command,
)
from coord.config import SmokeTestsConfig


# ── Part 1: the Test stage's command is declared, and its provenance travels ──


def _repo(**overrides) -> Repo:
    defaults = dict(name="portal", github="acme/portal")
    defaults.update(overrides)
    return Repo(**defaults)


class TestResolveSmokeCommand:
    def test_ci_command_outranks_every_other_source(self) -> None:
        """The whole point of #2091: when the repo declares what CI runs, the
        Test gate runs THAT, not the fast local subset."""
        resolved = resolve_smoke_command(
            _repo(test_command="npm test", ci_command="npm run test:e2e"),
            SmokeTestsConfig(default_command="make smoke"),
        )
        assert resolved.command == "npm run test:e2e"
        assert resolved.source == "repos[portal].ci_command"
        assert resolved.ci_equivalent is True

    def test_default_command_still_outranks_test_command(self) -> None:
        """Pre-existing #1021 precedence is preserved below the new field."""
        resolved = resolve_smoke_command(
            _repo(test_command="npm test"),
            SmokeTestsConfig(default_command="make smoke"),
        )
        assert resolved.command == "make smoke"
        assert resolved.source == "smoke_tests.default_command"
        assert resolved.ci_equivalent is False

    def test_test_command_is_the_last_resort_and_is_not_ci_equivalent(self) -> None:
        resolved = resolve_smoke_command(
            _repo(test_command="npm test"), SmokeTestsConfig()
        )
        assert resolved.command == "npm test"
        assert resolved.source == "repos[portal].test_command"
        assert resolved.ci_equivalent is False

    def test_nothing_configured_yields_no_command(self) -> None:
        resolved = resolve_smoke_command(_repo(), SmokeTestsConfig())
        assert resolved.command is None
        assert resolved.ci_equivalent is False

    def test_whitespace_only_ci_command_is_not_a_declaration(self) -> None:
        """A blank value must not silently become the Test-stage command (and
        must not claim CI-equivalence on the way)."""
        resolved = resolve_smoke_command(
            _repo(test_command="npm test", ci_command="   "), SmokeTestsConfig()
        )
        assert resolved.command == "npm test"
        assert resolved.ci_equivalent is False


class TestBriefingCarriesProvenance:
    def _briefing(self, resolved: SmokeCommand) -> str:
        return build_smoke_briefing(
            repo_github="acme/portal",
            repo_name="portal",
            branch="issue-14-x",
            issue_number=14,
            issue_title="Fix the thing",
            smoke_command=resolved.command or "",
            required_caps=[],
            timeout_seconds=600,
            is_worker=False,
            command_source=resolved,
        )

    def test_ci_equivalent_run_says_so(self) -> None:
        text = self._briefing(
            SmokeCommand("npm run test:e2e", "repos[portal].ci_command", True)
        )
        assert "CI-equivalent" in text
        assert "repos[portal].ci_command" in text

    def test_narrower_run_warns_the_verdict_is_not_ci(self) -> None:
        text = self._briefing(
            SmokeCommand("npm test", "repos[portal].test_command", False)
        )
        assert "NARROWER than CI" in text
        assert "#2091" in text

    def test_omitting_the_source_keeps_the_old_briefing(self) -> None:
        """Back-compat: callers that pass no provenance get no note rather
        than a misleading one."""
        text = build_smoke_briefing(
            repo_github="acme/portal", repo_name="portal", branch="b",
            issue_number=14, issue_title="t", smoke_command="npm test",
            required_caps=[], timeout_seconds=600, is_worker=False,
        )
        assert "NARROWER than CI" not in text
        assert "CI-equivalent" not in text


class TestCiCommandConfigParsing:
    def _write(self, tmp_path: Path, repo_block: str) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: portal\n"
            "    github: acme/portal\n"
            f"{repo_block}"
            "machines:\n"
            "  - name: laptop\n"
            "    host: laptop.tailnet\n"
            "    repos: [portal]\n"
            "    repo_paths:\n"
            "      portal: /tmp/portal\n"
        )
        return p

    def test_ci_command_round_trips(self, tmp_path: Path) -> None:
        cfg = load_config(
            self._write(tmp_path, '    ci_command: "npm run test:e2e"\n')
        )
        assert cfg.repo("portal").ci_command == "npm run test:e2e"

    def test_absent_ci_command_is_none(self, tmp_path: Path) -> None:
        cfg = load_config(self._write(tmp_path, '    test_command: "npm test"\n'))
        assert cfg.repo("portal").ci_command is None

    def test_non_string_ci_command_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="ci_command must be a string"):
            load_config(self._write(tmp_path, "    ci_command: 42\n"))

    def test_empty_ci_command_is_rejected(self, tmp_path: Path) -> None:
        """An empty string is a config mistake, not "no CI command" — reject
        it loudly rather than falling through to test_command."""
        with pytest.raises(ConfigError, match="non-empty string"):
            load_config(self._write(tmp_path, '    ci_command: ""\n'))


# ── Part 2: the live-CI read distinguishes "green" from "never looked" ───────


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
  type: github
"""

WORK_BRANCH = "issue-42-feature-x"


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


def _check(name: str, conclusion: str | None, status: str = "completed"):
    from coord.ci_store import CheckRun

    return CheckRun(
        name=name, status=status, conclusion=conclusion,
        url=f"https://github.com/acme/api/runs/{name}",
        run_id=name, started_at=None, completed_at=None,
    )


@pytest.fixture
def ci_config(tmp_path: Path) -> Path:
    p = tmp_path / "ci.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coord_db) -> Path:
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state_mod, "COORD_DIR", d)
    return d


@pytest.fixture(autouse=True)
def _machine_always_reachable(monkeypatch: pytest.MonkeyPatch):
    """#3208: `coord fix` now probes reachability (`coord.dispatch.
    select_fix_machine`, via `coord.network.fetch_status`) before picking a
    machine. This module's `laptop.tailnet` host doesn't actually resolve —
    default it to reachable so these CI-parity tests keep exercising CI
    parity, not machine selection."""
    from coord.network import StatusResult

    monkeypatch.setattr(
        "coord.network.fetch_status",
        lambda machine, timeout=3.0: StatusResult(data={}),
    )


def _cfg(path: Path):
    return load_config(path)


class TestCiReadReasons:
    """``_read_ci`` must name the short-circuit it took. Every one of these
    returned a bare ``None`` before #2091, which the caller rendered as "no
    red CI on its PR" — the exact sentence that misled the operator while
    ``gh pr checks 42`` said `fail`."""

    def test_unknown_repo(self, ci_config: Path) -> None:
        from coord.commands.plan_followup import _read_ci

        read = _read_ci(_cfg(ci_config), _work(repo_name="not-configured"))
        assert not read.was_read
        assert "not in coordinator.yml" in read.unread_reason

    def test_ci_store_disabled(self, tmp_path: Path) -> None:
        from coord.commands.plan_followup import _read_ci

        p = tmp_path / "none.yml"
        p.write_text(CONFIG_YAML.replace("type: github", "type: none"))
        read = _read_ci(_cfg(p), _work())
        assert not read.was_read
        assert "ci_store.type is 'none'" in read.unread_reason

    def test_no_pr_resolvable(self, ci_config: Path, monkeypatch) -> None:
        from coord.commands.plan_followup import _read_ci

        fake_store = MagicMock()
        fake_store.is_available = True
        monkeypatch.setattr("coord.ci_store.build_ci_store", lambda _t, **_kw: fake_store)
        monkeypatch.setattr("coord.github_ops.find_pr_for_branch", lambda *_a: None)

        read = _read_ci(_cfg(ci_config), _work(pr_url=""))
        assert not read.was_read
        assert "no PR could be resolved" in read.unread_reason
        fake_store.list_checks_for_pr.assert_not_called()

    def test_pr_url_missing_falls_back_to_the_branch(
        self, ci_config: Path, monkeypatch
    ) -> None:
        """A row whose PR was opened out of band used to disable the whole
        fallback. Ask GitHub which PR has this branch as its head instead."""
        from coord.commands.plan_followup import _read_ci

        fake_store = MagicMock()
        fake_store.is_available = True
        fake_store.list_checks_for_pr.return_value = [
            _check("e2e smoke (playwright)", "failure")
        ]
        monkeypatch.setattr("coord.ci_store.build_ci_store", lambda _t, **_kw: fake_store)
        monkeypatch.setattr(
            "coord.github_ops.find_pr_for_branch", lambda *_a: {"number": 42}
        )

        read = _read_ci(_cfg(ci_config), _work(pr_url=""))
        assert read.is_red
        assert read.pr_number == 42
        assert "e2e smoke (playwright)" in read.story
        fake_store.list_checks_for_pr.assert_called_once_with("acme/api", 42)

    def test_read_raises(self, ci_config: Path, monkeypatch) -> None:
        from coord.commands.plan_followup import _read_ci

        fake_store = MagicMock()
        fake_store.is_available = True
        fake_store.list_checks_for_pr.side_effect = RuntimeError("gh exploded")
        monkeypatch.setattr("coord.ci_store.build_ci_store", lambda _t, **_kw: fake_store)

        read = _read_ci(_cfg(ci_config), _work())
        assert not read.was_read
        assert "gh exploded" in read.unread_reason

    def test_no_checks_at_all(self, ci_config: Path, monkeypatch) -> None:
        from coord.commands.plan_followup import _read_ci

        fake_store = MagicMock()
        fake_store.is_available = True
        fake_store.list_checks_for_pr.return_value = []
        monkeypatch.setattr("coord.ci_store.build_ci_store", lambda _t, **_kw: fake_store)

        read = _read_ci(_cfg(ci_config), _work())
        assert not read.was_read
        assert "no checks at all" in read.unread_reason

    def test_all_checks_still_running_is_not_green(
        self, ci_config: Path, monkeypatch
    ) -> None:
        """The 44m57s CI run was still in flight for most of the window in
        which the Test gate went green. Pending is not a verdict."""
        from coord.commands.plan_followup import _read_ci

        fake_store = MagicMock()
        fake_store.is_available = True
        fake_store.list_checks_for_pr.return_value = [
            _check("e2e smoke (playwright)", None, status="in_progress")
        ]
        monkeypatch.setattr("coord.ci_store.build_ci_store", lambda _t, **_kw: fake_store)

        read = _read_ci(_cfg(ci_config), _work())
        assert not read.was_read
        assert "still running" in read.unread_reason

    def test_a_genuine_green_read_is_marked_as_read(
        self, ci_config: Path, monkeypatch
    ) -> None:
        from coord.commands.plan_followup import _read_ci

        fake_store = MagicMock()
        fake_store.is_available = True
        fake_store.list_checks_for_pr.return_value = [_check("build", "success")]
        monkeypatch.setattr("coord.ci_store.build_ci_store", lambda _t, **_kw: fake_store)

        read = _read_ci(_cfg(ci_config), _work())
        assert read.was_read
        assert not read.is_red
        assert read.pr_number == 7


class TestFixSurfacesWhyCiWasNotRead:
    def test_refusal_names_the_short_circuit(
        self, tmp_path: Path, coord_dir: Path
    ) -> None:
        """The observed refusal said only "expected a failed test verdict (or
        red CI on its PR...)". With ci_store off, no CI read ever happened —
        say that, so the operator is not left believing CI is green."""
        p = tmp_path / "none.yml"
        p.write_text(CONFIG_YAML.replace("type: github", "type: none"))
        state_mod.save_board(Board(completed=[_work(test_state="passed", smoke_test="pass")]))

        result = CliRunner().invoke(main, ["fix", "work-abc", "--config", str(p)])

        assert result.exit_code != 0
        assert "live CI was NOT read" in result.output
        assert "ci_store.type is 'none'" in result.output

    def test_a_genuine_green_read_says_it_was_read(
        self, ci_config: Path, coord_dir: Path, monkeypatch
    ) -> None:
        state_mod.save_board(Board(completed=[_work(test_state="passed", smoke_test="pass")]))
        fake_store = MagicMock()
        fake_store.is_available = True
        fake_store.list_checks_for_pr.return_value = [_check("build", "success")]
        monkeypatch.setattr("coord.ci_store.build_ci_store", lambda _t, **_kw: fake_store)

        result = CliRunner().invoke(
            main, ["fix", "work-abc", "--config", str(ci_config)]
        )

        assert result.exit_code != 0
        assert "was read and reported no failing completed check" in result.output
        assert "live CI was NOT read" not in result.output


class TestStoredPassedVsLiveRedIsAConflict:
    def test_conflict_is_announced_when_the_fix_dispatches(
        self, ci_config: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """A stored `passed` verdict on a branch whose CI is red is not a
        detail of this dispatch — the two "the tests" disagree. Name the
        conflict instead of silently preferring the CI read.

        #2244: the message must NOT assert a cause it hasn't measured. It
        used to state flatly that "the Test stage ran a narrower suite than
        CI"; on #2230 the Test stage ran the FULL suite and the same five
        tests failed in both — the green verdict came from the headless
        smoke's unwired verdict channel. `ci_command` stays in the message as
        one candidate to check, not as the diagnosis."""
        state_mod.save_board(Board(completed=[_work(test_state="passed", smoke_test="pass")]))

        fake_store = MagicMock()
        fake_store.is_available = True
        fake_store.list_checks_for_pr.return_value = [
            _check("e2e smoke (playwright)", "failure")
        ]
        monkeypatch.setattr("coord.ci_store.build_ci_store", lambda _t, **_kw: fake_store)

        with patch("coord.dispatch.dispatch", return_value={"id": "fix-ci"}), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["fix", "work-abc", "--config", str(ci_config)]
            )

        assert result.exit_code == 0, result.output
        assert "conflict (#2091)" in result.output
        assert "CI on PR #7 is RED" in result.output
        assert "repos[api].ci_command" in result.output
        # #2244: no unmeasured diagnosis, and it points at the evidence.
        assert "ran a narrower suite than CI" not in result.output
        assert "coord log work-abc" in result.output

    def test_no_conflict_line_when_the_stored_verdict_already_failed(
        self, ci_config: Path, coord_dir: Path, monkeypatch
    ) -> None:
        """A failed verdict agreeing with red CI is not a conflict — and the
        cheap in-DB path must stay zero-I/O, so CI is never even read."""
        state_mod.save_board(Board(completed=[_work(test_state="failed", smoke_test="fail")]))

        def _boom(_t, **_kw):
            raise AssertionError("CI must not be read when the verdict failed")

        monkeypatch.setattr("coord.ci_store.build_ci_store", _boom)

        with patch("coord.dispatch.dispatch", return_value={"id": "fix"}), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["fix", "work-abc", "--config", str(ci_config)]
            )

        assert result.exit_code == 0, result.output
        assert "conflict (#2091)" not in result.output
