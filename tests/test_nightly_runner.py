"""Tests for coord/nightly_runner.py — the #3660 I/O shell that wires
#3652's decision core (coord.nightly_smoke) into a real, end-to-end run.

Mirrors the acceptance criteria named in the issue:

- a green run files no issue and touches no runner call at all,
- a red run files exactly one issue,
- a repeat red run against the SAME step updates that SAME issue,
- a known-bug step going green alerts (closes the parked issue),
- a host that fails the GUI-lane pre-flight reports INFRA and files
  nothing — never a plain app red.

Plus the surrounding plumbing: artifact-plan resolution (integration branch
vs. release tag), the GUI-lane pre-flight read off a live `/health`, and
the persisted nightly-results store round-tripping through
`coord.release_gate.NightlyArtifactResult`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coord.nightly_runner as nightly_runner
from coord.acceptance_drivers import DriverResult
from coord.config import AcceptanceConfig, AcceptanceDriverConfig, Config, SmokeTestsConfig
from coord.models import Board, Machine, Repo
from coord.nightly_runner import (
    NightlyRunnerError,
    NightlyRunPlan,
    ObtainedArtifact,
    _default_download_artifact,
    _git_clone_ref,
    _git_ref_for_plan,
    _resolve_bugbash_lane,
    gui_lane_preflight_blockers,
    known_bugs_from_spec_text,
    observations_from_driver_result,
    plan_nightly_run,
    resolve_nightly_artifact_plan,
    run_nightly_smoke,
)
from coord.nightly_store import read_nightly_results


def _machine(name: str, *, caps: list[str]) -> Machine:
    return Machine(name=name, host=f"{name}.tail", capabilities=caps, repos=["vimcode"])


def _config(
    *, develop_branch: str | None = "integration", kind: str = "cli-pytest",
    capability: str = "python", caps: list[str] | None = None,
) -> Config:
    return Config(
        repos=[Repo(name="vimcode", github="acme/vimcode", develop_branch=develop_branch)],
        machines=[_machine("m1", caps=caps if caps is not None else [capability])],
        smoke_tests=SmokeTestsConfig(auto_queue=True),
        acceptance=AcceptanceConfig(drivers={
            "vimcode": AcceptanceDriverConfig(
                kind=kind, run="pytest", capability=capability,
                entrypoint="tests/smoke-spec/install.yaml",
            ),
        }),
    )


class _FakeHealthResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeHealthClient:
    def __init__(self, health: dict[str, dict]) -> None:
        self._health = health

    @staticmethod
    def _host(url: str) -> str:
        return url.split("//", 1)[1].split(":", 1)[0]

    def get(self, url, *, timeout) -> _FakeHealthResp:
        return _FakeHealthResp(self._health.get(self._host(url), {}))


def _gui_lane_health(*, crit: bool, lane: str = "mac-native") -> dict:
    return {
        "health": {
            "results": [
                {
                    "check_id": "gui_lane_preflight",
                    "subject": lane,
                    "severity": "crit" if crit else "ok",
                    "headroom": "INFRA: screen is locked" if crit else "ready",
                    "detail": "unlock it" if crit else "",
                }
            ]
        }
    }


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._next_issue = 100

    def __call__(self, args) -> str:
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["issue", "create"]:
            number = self._next_issue
            self._next_issue += 1
            return f"created https://github.com/acme/vimcode/issues/{number} (#{number})"
        return ""


# ── resolve_nightly_artifact_plan ──────────────────────────────────────────


class TestResolveNightlyArtifactPlan:
    def test_integration_branch_is_preferred_and_never_fetches_a_tag(self) -> None:
        config = _config(develop_branch="integration")

        def _boom(_slug: str) -> str:
            raise AssertionError("must not fetch a release tag when develop_branch is set")

        plan = resolve_nightly_artifact_plan(
            repo="vimcode", artifact="macos-dmg", config=config,
            fetch_latest_release_tag_fn=_boom,
        )
        assert plan.source.value == "build"
        assert plan.ref == "integration"

    def test_falls_back_to_fetched_release_tag(self) -> None:
        config = _config(develop_branch=None)
        plan = resolve_nightly_artifact_plan(
            repo="vimcode", artifact="macos-dmg", config=config,
            fetch_latest_release_tag_fn=lambda slug: "0.15.0",
        )
        assert plan.source.value == "download"
        assert plan.ref == "0.15.0"

    def test_unknown_repo_raises(self) -> None:
        config = _config()
        with pytest.raises(NightlyRunnerError, match="not declared"):
            resolve_nightly_artifact_plan(repo="nope", artifact="x", config=config)


# ── gui_lane_preflight_blockers ─────────────────────────────────────────────


class TestGuiLanePreflightBlockers:
    def test_crit_result_is_a_blocker(self) -> None:
        client = _FakeHealthClient({"m1.tail": _gui_lane_health(crit=True)})
        blockers = gui_lane_preflight_blockers(
            _machine("m1", caps=["macos"]), "mac-native", http_client=client,
        )
        assert blockers
        assert "locked" in blockers[0]

    def test_ok_result_is_not_a_blocker(self) -> None:
        client = _FakeHealthClient({"m1.tail": _gui_lane_health(crit=False)})
        blockers = gui_lane_preflight_blockers(
            _machine("m1", caps=["macos"]), "mac-native", http_client=client,
        )
        assert blockers == []

    def test_no_result_at_all_is_not_a_blocker(self) -> None:
        """#3651's own silence-gap note: an agent predating the probe must
        not refuse every nightly dispatch on missing telemetry."""
        client = _FakeHealthClient({})
        blockers = gui_lane_preflight_blockers(
            _machine("m1", caps=["macos"]), "mac-native", http_client=client,
        )
        assert blockers == []


# ── plan_nightly_run ────────────────────────────────────────────────────────


class TestPlanNightlyRun:
    def test_no_driver_configured_raises(self) -> None:
        config = Config(
            repos=[Repo(name="vimcode", github="acme/vimcode")],
            machines=[_machine("m1", caps=["python"])],
        )
        with pytest.raises(NightlyRunnerError, match="no acceptance driver"):
            plan_nightly_run(
                repo="vimcode", artifact="macos-dmg", spec="", config=config, board=Board(),
            )

    def test_non_gui_driver_never_checks_gui_preflight(self) -> None:
        config = _config(kind="cli-pytest", capability="python")
        plan = plan_nightly_run(
            repo="vimcode", artifact="macos-dmg", spec="", config=config, board=Board(),
        )
        assert plan.infra_blocked is False
        assert plan.machine_name == "m1"

    def test_no_capable_host_is_infra_blocked(self) -> None:
        config = _config(capability="macos", caps=["python"])
        plan = plan_nightly_run(
            repo="vimcode", artifact="macos-dmg", spec="", config=config, board=Board(),
        )
        assert plan.host is None
        assert plan.infra_blocked is True
        assert "nothing qualifies" in plan.infra_reason

    def test_gui_driver_with_crit_preflight_is_infra_blocked(self) -> None:
        config = _config(kind="mac-native", capability="macos", caps=["macos"])
        client = _FakeHealthClient({"m1.tail": _gui_lane_health(crit=True)})
        plan = plan_nightly_run(
            repo="vimcode", artifact="macos-dmg", spec="", config=config, board=Board(),
            http_client=client,
        )
        assert plan.infra_blocked is True
        assert "locked" in plan.infra_reason

    def test_gui_driver_with_ok_preflight_is_not_blocked(self) -> None:
        config = _config(kind="mac-native", capability="macos", caps=["macos"])
        client = _FakeHealthClient({"m1.tail": _gui_lane_health(crit=False)})
        plan = plan_nightly_run(
            repo="vimcode", artifact="macos-dmg", spec="", config=config, board=Board(),
            http_client=client,
        )
        assert plan.infra_blocked is False

    def test_render_mentions_source_host_and_readiness(self) -> None:
        config = _config()
        plan = plan_nightly_run(
            repo="vimcode", artifact="macos-dmg", spec="", config=config, board=Board(),
        )
        text = plan.render()
        assert "build" in text
        assert "m1" in text
        assert "ready to run" in text


# ── known_bugs_from_spec_text ───────────────────────────────────────────────


class TestKnownBugsFromSpecText:
    def test_maps_step_id_to_ref(self) -> None:
        text = """
steps:
  - id: launch
  - id: uninstall
    known_bug: vimcode#42
"""
        assert known_bugs_from_spec_text(text) == {"uninstall": "vimcode#42"}

    def test_malformed_yaml_returns_empty(self) -> None:
        assert known_bugs_from_spec_text("not: valid: yaml: [") == {}

    def test_non_mapping_document_returns_empty(self) -> None:
        assert known_bugs_from_spec_text("- just\n- a\n- list\n") == {}


# ── observations_from_driver_result ─────────────────────────────────────────


class TestObservationsFromDriverResult:
    def test_pass_fail_and_unavailable(self) -> None:
        result = DriverResult(exit_code=1, tests=[
            {"id": "launch", "status": "pass"},
            {"id": "uninstall", "status": "fail", "message": "crashed"},
            {"id": "menu", "status": "unavailable", "message": "locked"},
        ])
        out = observations_from_driver_result(
            result, repo="vimcode", spec="install.yaml", sha="deadbeef", checked_at=1.0,
        )
        by_step = {obs.step: (obs, unavailable) for obs, unavailable in out}
        assert by_step["launch"][0].passed is True
        assert by_step["launch"][1] is False
        assert by_step["uninstall"][0].passed is False
        assert by_step["uninstall"][1] is False
        assert by_step["menu"][0].passed is False
        assert by_step["menu"][1] is True

    def test_no_tests_at_all_still_yields_one_failing_observation(self) -> None:
        """#2096: a crash with zero structured output must never read as
        'zero steps, therefore nothing failed'."""
        result = DriverResult(exit_code=1, tests=[], raw_output="boom")
        out = observations_from_driver_result(
            result, repo="vimcode", spec="install.yaml", sha="deadbeef", checked_at=1.0,
        )
        assert len(out) == 1
        obs, unavailable = out[0]
        assert obs.passed is False
        assert unavailable is False

    def test_no_tests_at_all_fails_even_with_exit_code_zero(self) -> None:
        """#3660 review round 1: `passed=result.ok` let a `run:` wrapper
        that exits 0 without ever emitting a parseable report (or a native
        spec whose `steps:` is empty) certify the artifact with zero steps
        ever observed. "No structured results at all" must fail
        irrespective of exit code."""
        result = DriverResult(exit_code=0, tests=[])
        out = observations_from_driver_result(
            result, repo="vimcode", spec="install.yaml", sha="deadbeef", checked_at=1.0,
        )
        assert len(out) == 1
        obs, unavailable = out[0]
        assert obs.passed is False
        assert unavailable is False


# ── run_nightly_smoke: the end-to-end acceptance scenarios ─────────────────


_SPEC_YAML = """
steps:
  - id: launch
  - id: uninstall
    known_bug: vimcode#42
"""


def _write_spec(tmp_path: Path) -> None:
    spec_dir = tmp_path / "tests" / "smoke-spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "install.yaml").write_text(_SPEC_YAML)


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, tests: list[dict], config: Config | None = None,
    runner: _FakeRunner | None = None, open_issues: list[dict] | None = None,
    closed_issues: list[dict] | None = None,
):
    monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))
    _write_spec(tmp_path)
    cfg = config or _config()
    runner = runner or _FakeRunner()
    report = run_nightly_smoke(
        repo="vimcode", artifact="macos-dmg", spec="", config=cfg, board=Board(),
        dry_run=False,
        resolve_ref_sha_fn=lambda slug, ref: "deadbeef",
        local_machine_name_fn=lambda cfg: "m1",
        obtain_artifact_fn=lambda plan, *, config, workdir: ObtainedArtifact(cwd=str(tmp_path)),
        run_driver_fn=lambda kind, run_command, cwd, **kw: DriverResult(exit_code=0, tests=tests),
        runner=runner,
        open_issues=open_issues or [],
        closed_issues=closed_issues or [],
        now=1000.0,
    )
    return report, runner


class TestRunNightlySmokeDryRunAndInfra:
    def test_dry_run_does_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))
        config = _config()
        report = run_nightly_smoke(
            repo="vimcode", artifact="macos-dmg", spec="", config=config, board=Board(),
            dry_run=True,
        )
        assert report.ran is False
        assert read_nightly_results("vimcode") == []

    def test_infra_blocked_host_never_runs_or_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#3660 acceptance: a host that fails pre-flight is INFRA, not an
        app red — never runs the spec, never files an issue."""
        monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))
        config = _config(kind="mac-native", capability="macos", caps=["macos"])
        client = _FakeHealthClient({"m1.tail": _gui_lane_health(crit=True)})
        runner = _FakeRunner()
        report = run_nightly_smoke(
            repo="vimcode", artifact="macos-dmg", spec="", config=config, board=Board(),
            dry_run=False, http_client=client, runner=runner,
            resolve_ref_sha_fn=lambda slug, ref: "deadbeef",
        )
        assert report.ran is False
        assert report.infra_blocked is True
        assert runner.calls == []

    def test_infra_blocked_host_persists_a_distinct_unavailable_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#3660 review non-blocking item 2: an INFRA-blocked plan must
        record a distinct "unavailable" verdict, not silently persist
        nothing at all (which the release gate can't tell apart from a
        repo nobody has ever run)."""
        monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))
        config = _config(kind="mac-native", capability="macos", caps=["macos"])
        client = _FakeHealthClient({"m1.tail": _gui_lane_health(crit=True)})
        report = run_nightly_smoke(
            repo="vimcode", artifact="macos-dmg", spec="", config=config, board=Board(),
            dry_run=False, http_client=client, runner=_FakeRunner(),
            resolve_ref_sha_fn=lambda slug, ref: "deadbeef",
        )
        assert report.ran is False
        results = read_nightly_results("vimcode")
        assert len(results) == 1
        assert results[0].unavailable is True
        assert results[0].passed is False
        assert "locked" in results[0].detail

    def test_dry_run_never_persists_even_when_infra_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))
        config = _config(kind="mac-native", capability="macos", caps=["macos"])
        client = _FakeHealthClient({"m1.tail": _gui_lane_health(crit=True)})
        report = run_nightly_smoke(
            repo="vimcode", artifact="macos-dmg", spec="", config=config, board=Board(),
            dry_run=True, http_client=client,
        )
        assert report.ran is False
        assert read_nightly_results("vimcode") == []


class TestRunNightlySmokeGreenRedKnownBug:
    def test_green_run_files_nothing_and_never_calls_the_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report, runner = _run(
            tmp_path, monkeypatch,
            tests=[{"id": "launch", "status": "pass"}],
        )
        assert report.ran is True
        assert len(report.outcomes) == 1
        assert report.outcomes[0].action == "none"
        assert runner.calls == []  # #3652 "no LLM on a green run"
        results = read_nightly_results("vimcode")
        assert len(results) == 1
        assert results[0].passed is True

    def test_red_run_files_exactly_one_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report, runner = _run(
            tmp_path, monkeypatch,
            tests=[{"id": "launch", "status": "fail", "message": "window never appeared"}],
        )
        assert report.ran is True
        assert report.outcomes[0].action == "filed"
        assert report.outcomes[0].issue_number is not None
        creates = [c for c in runner.calls if c[:2] == ["issue", "create"]]
        assert len(creates) == 1

    def test_repeat_red_run_updates_the_same_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#3652 Wanted #2: a repeated failure of the SAME (platform, spec,
        step) comments on the existing issue rather than filing a new one."""
        open_issues = [{
            "number": 7,
            "title": "[bugbash:cli-pytest] [nightly:cli-pytest|tests/smoke-spec/install.yaml|launch] "
                     "nightly smoke: launch failing",
        }]
        report, runner = _run(
            tmp_path, monkeypatch,
            tests=[{"id": "launch", "status": "fail", "message": "window never appeared"}],
            open_issues=open_issues,
        )
        assert report.outcomes[0].action == "commented"
        assert report.outcomes[0].issue_number == 7
        creates = [c for c in runner.calls if c[:2] == ["issue", "create"]]
        assert creates == []
        comments = [c for c in runner.calls if c[:2] == ["issue", "comment"]]
        assert len(comments) == 1
        assert comments[0][3] == "7"

    def test_known_bug_step_still_red_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report, runner = _run(
            tmp_path, monkeypatch,
            tests=[{"id": "uninstall", "status": "fail", "message": "still broken"}],
        )
        assert report.outcomes[0].action == "none"
        assert runner.calls == []

    def test_known_bug_step_going_green_alerts_and_closes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#3652 Wanted #3: the bidirectional half of the known-bug gate."""
        report, runner = _run(
            tmp_path, monkeypatch,
            tests=[{"id": "uninstall", "status": "pass"}],
        )
        assert report.outcomes[0].action == "closed"
        assert report.outcomes[0].issue_number == 42
        closes = [c for c in runner.calls if c[:2] == ["issue", "close"]]
        assert len(closes) == 1
        assert closes[0][3] == "42"

    def test_unavailable_step_files_nothing_and_is_recorded_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#3660 review round 1: a driver-reported `unavailable` step
        (#3510 — a locked/absent GUI session, not an app bug) must never
        be classified into a filing decision, mirroring
        `coord.bugbash`'s own "an unavailable lane is not a bug finding."
        """
        report, runner = _run(
            tmp_path, monkeypatch,
            tests=[{"id": "launch", "status": "unavailable", "message": "screen locked"}],
        )
        assert report.ran is True
        assert report.outcomes[0].action == "none"
        assert runner.calls == []  # never files, comments, or closes
        results = read_nightly_results("vimcode")
        assert len(results) == 1
        assert results[0].unavailable is True
        assert results[0].passed is False

    def test_unavailable_step_is_named_in_the_report_not_silently_quiet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#3566 / #3660 review round 2: a step that never ran must not be
        indistinguishable from a clean green in the runner's own report —
        `coord.bugbash` reports `lanes_unavailable` for exactly this
        reason."""
        report, _runner = _run(
            tmp_path, monkeypatch,
            tests=[
                {"id": "launch", "status": "pass"},
                {"id": "menu", "status": "unavailable", "message": "screen locked"},
            ],
        )
        assert report.any_unavailable is True
        assert report.unavailable_steps == ("tests/smoke-spec/install.yaml::menu",)
        assert report.any_app_red is False

    def test_a_fully_green_run_reports_nothing_unavailable_and_no_red(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report, _runner = _run(
            tmp_path, monkeypatch, tests=[{"id": "launch", "status": "pass"}],
        )
        assert report.any_unavailable is False
        assert report.any_app_red is False
        assert report.any_dropped is False
        assert report.lane_fallback  # cli-pytest has no bugbash lane

    def test_a_red_run_reports_an_app_red(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report, _runner = _run(
            tmp_path, monkeypatch,
            tests=[{"id": "launch", "status": "fail", "message": "crashed"}],
        )
        assert report.any_app_red is True
        assert report.any_unavailable is False

    def test_wrong_host_refuses_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#966's own settled answer, mirrored here: never silently run on
        the wrong hardware."""
        monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))
        config = _config()
        with pytest.raises(NightlyRunnerError, match="run this command on"):
            run_nightly_smoke(
                repo="vimcode", artifact="macos-dmg", spec="", config=config, board=Board(),
                dry_run=False,
                local_machine_name_fn=lambda cfg: "some-other-machine",
            )


class TestAnInterruptedRunNeverCertifies:
    """#3660 review round 2: the loop used to ACT first and PERSIST second,
    so a 2-step run whose second step was a brand-new red lost that red
    entirely when `file_finding` raised (`subprocess_coord_runner` raises
    on any non-zero `coord` exit, and nothing here catches it) — leaving a
    per-run group holding only the first, PASSING row, which reduced to
    "1 step(s) passed" with a fresh timestamp and reported
    `nightly:macos-dmg PASS` for an artifact whose red step WAS observed.
    """

    def test_a_runner_that_raises_on_issue_create_leaves_the_gate_red(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from coord.nightly_store import nightly_artifact_results_for_release_gate
        from coord.release_gate import evaluate_release_gate

        class _RaisingRunner:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def __call__(self, args) -> str:
                args = list(args)
                self.calls.append(args)
                if args[:2] == ["issue", "create"]:
                    raise RuntimeError("coord issue create exited 1")
                return ""

        monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))
        _write_spec(tmp_path)
        runner = _RaisingRunner()
        tests = [
            {"id": "launch", "status": "pass"},
            {"id": "menu", "status": "fail", "message": "crashed"},
        ]
        with pytest.raises(RuntimeError, match="issue create"):
            run_nightly_smoke(
                repo="vimcode", artifact="macos-dmg", spec="", config=_config(),
                board=Board(), dry_run=False,
                resolve_ref_sha_fn=lambda slug, ref: "deadbeef",
                local_machine_name_fn=lambda cfg: "m1",
                obtain_artifact_fn=lambda plan, *, config, workdir: ObtainedArtifact(
                    cwd=str(tmp_path),
                ),
                run_driver_fn=lambda kind, run_command, cwd, **kw: DriverResult(
                    exit_code=1, tests=tests,
                ),
                runner=runner, open_issues=[], closed_issues=[], now=1000.0,
            )

        # (a) the red observation was persisted BEFORE the filing attempt.
        rows = read_nightly_results("vimcode")
        by_step = {r.step: r for r in rows}
        assert by_step["menu"].passed is False
        assert by_step["launch"].passed is True

        # ...and the gate is not passing at that SHA.
        verdict = evaluate_release_gate(
            repo="vimcode", release_sha="deadbeef",
            required_lanes=[], nightly_required=True,
            required_nightly_artifacts=["macos-dmg"],
            nightly_results=nightly_artifact_results_for_release_gate("vimcode"),
        )
        assert verdict.gate_passed is False

    def test_every_persisted_row_states_the_runs_total_step_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(b) the completeness marker the store needs to refuse to grade a
        group that holds fewer rows than the run promised — known from
        `len(observations)` before the act/persist loop starts."""
        report, _runner = _run(
            tmp_path, monkeypatch,
            tests=[
                {"id": "launch", "status": "pass"},
                {"id": "uninstall", "status": "pass"},
            ],
        )
        assert report.ran is True
        rows = read_nightly_results("vimcode")
        assert len(rows) == 2
        assert {r.steps_total for r in rows} == {2}
        assert len({r.run_id for r in rows}) == 1

    def test_a_run_killed_before_its_last_step_cannot_pass_the_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A kill/Ctrl-C between two steps (simulated by making the SECOND
        `record_nightly_result` raise, as an interrupt would) leaves the
        store holding only the first, passing row — which must read as
        "the run did not finish", never as a pass."""
        from coord.nightly_store import nightly_artifact_results_for_release_gate
        from coord.release_gate import evaluate_release_gate

        import coord.nightly_store as nightly_store

        real_record = nightly_store.record_nightly_result
        seen: list[str] = []

        def _record_then_die(record) -> None:
            if seen:
                raise KeyboardInterrupt("operator hit Ctrl-C")
            seen.append(record.step)
            real_record(record)

        monkeypatch.setattr(nightly_runner, "record_nightly_result", _record_then_die)
        monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))
        _write_spec(tmp_path)
        with pytest.raises(KeyboardInterrupt):
            run_nightly_smoke(
                repo="vimcode", artifact="macos-dmg", spec="", config=_config(),
                board=Board(), dry_run=False,
                resolve_ref_sha_fn=lambda slug, ref: "deadbeef",
                local_machine_name_fn=lambda cfg: "m1",
                obtain_artifact_fn=lambda plan, *, config, workdir: ObtainedArtifact(
                    cwd=str(tmp_path),
                ),
                run_driver_fn=lambda kind, run_command, cwd, **kw: DriverResult(
                    exit_code=0,
                    tests=[
                        {"id": "launch", "status": "pass"},
                        {"id": "menu", "status": "pass"},
                    ],
                ),
                runner=_FakeRunner(), open_issues=[], closed_issues=[], now=1000.0,
            )

        results = nightly_artifact_results_for_release_gate("vimcode")
        assert len(results) == 1
        assert results[0].passed is False
        assert "did not finish" in results[0].detail
        verdict = evaluate_release_gate(
            repo="vimcode", release_sha="deadbeef",
            required_lanes=[], nightly_required=True,
            required_nightly_artifacts=["macos-dmg"],
            nightly_results=nightly_artifact_results_for_release_gate("vimcode"),
        )
        assert verdict.gate_passed is False


class TestIoFailuresBecomeRunnerErrors:
    """#3660 review: `git clone`/`build_command` raise
    `subprocess.CalledProcessError`, `fetch_release_assets` raises `httpx`
    errors, and `run_driver` raises `DriverError` — the CLI catches only
    `NightlyRunnerError`, so an operator got a bare traceback instead of
    the `exit 2` every other failure mode produces."""

    def _call(self, tmp_path: Path, **overrides):
        kwargs = dict(
            repo="vimcode", artifact="macos-dmg", spec="", config=_config(),
            board=Board(), dry_run=False,
            resolve_ref_sha_fn=lambda slug, ref: "deadbeef",
            local_machine_name_fn=lambda cfg: "m1",
            obtain_artifact_fn=lambda plan, *, config, workdir: ObtainedArtifact(
                cwd=str(tmp_path),
            ),
            run_driver_fn=lambda kind, run_command, cwd, **kw: DriverResult(
                exit_code=0, tests=[{"id": "launch", "status": "pass"}],
            ),
            runner=_FakeRunner(), now=1000.0,
        )
        kwargs.update(overrides)
        return run_nightly_smoke(**kwargs)

    def test_a_failing_build_becomes_a_runner_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))
        _write_spec(tmp_path)

        def _boom(plan, *, config, workdir):
            raise subprocess.CalledProcessError(128, ["git", "clone"])

        with pytest.raises(NightlyRunnerError, match="could not obtain"):
            self._call(tmp_path, obtain_artifact_fn=_boom)

    def test_a_driver_error_becomes_a_runner_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from coord.acceptance_drivers import DriverError

        monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))
        _write_spec(tmp_path)

        def _boom(kind, run_command, cwd, **kw):
            raise DriverError("timed out after 900s")

        with pytest.raises(NightlyRunnerError, match="could not run"):
            self._call(tmp_path, run_driver_fn=_boom)


# ── _git_ref_for_plan: DOWNLOAD plans need the real `v`-prefixed tag ───────


def _plan(*, source: str, ref: str, driver_kind: str = "mac-native") -> NightlyRunPlan:
    return NightlyRunPlan(
        repo="vimcode", artifact="macos-dmg", spec="", driver_kind=driver_kind,
        source=source, ref=ref, detail="", host=None, host_rationale="",
        infra_blocked=False, infra_reason="",
    )


class TestGitRefForPlan:
    def test_download_plan_gets_a_v_prefix(self) -> None:
        """#3660 review round 1: `fetch_latest_release_tag` deliberately
        returns a BARE version — `git ls-remote`/`git clone` need the real
        tag name."""
        assert _git_ref_for_plan(_plan(source="download", ref="0.15.0")) == "v0.15.0"

    def test_download_plan_already_v_prefixed_is_left_alone(self) -> None:
        assert _git_ref_for_plan(_plan(source="download", ref="v0.15.0")) == "v0.15.0"

    def test_build_plan_branch_name_is_never_prefixed(self) -> None:
        assert _git_ref_for_plan(_plan(source="build", ref="integration")) == "integration"


# ── _git_clone_ref: reuse an existing checkout rather than re-cloning ──────


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _local_repo_with_two_tagged_commits(tmp_path: Path) -> Path:
    src = tmp_path / "src.git"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "t")
    (src / "f.txt").write_text("v1")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "v1")
    _git(src, "tag", "v1.0.0")
    (src / "f.txt").write_text("v2")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "v2")
    _git(src, "tag", "v2.0.0")
    return src


class TestGitCloneRefReuse:
    def test_second_call_reuses_the_checkout_instead_of_failing(
        self, tmp_path: Path,
    ) -> None:
        """#3660 review round 1: a bare `git clone` refuses a non-empty
        destination, and `_default_workdir` deliberately returns the SAME
        path every night for a given repo — so a repeat nightly run must
        not require the caller to clean the workdir by hand."""
        src = _local_repo_with_two_tagged_commits(tmp_path)
        workdir = tmp_path / "work"
        remote_url = f"file://{src}"

        _git_clone_ref(remote_url, "v1.0.0", str(workdir))
        assert (workdir / "f.txt").read_text() == "v1"

        # The second call, at a DIFFERENT ref, must succeed (not raise
        # CalledProcessError("destination path already exists")) and leave
        # the checkout actually updated.
        _git_clone_ref(remote_url, "v2.0.0", str(workdir))
        assert (workdir / "f.txt").read_text() == "v2"


# ── _default_download_artifact: Path, not str, into download_asset ────────


class TestDefaultDownloadArtifact:
    def test_passes_a_path_to_download_asset_not_a_str(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#3660 review round 1: `coord.tui_release.download_asset(url,
        dest_dir: Path)` immediately calls `dest_dir.mkdir(...)` — passing
        a bare `str` raised `AttributeError` on every download run."""
        import coord.tui_release as tui_release

        monkeypatch.setattr(nightly_runner, "_git_clone_ref", lambda *a, **k: None)
        monkeypatch.setattr(
            tui_release, "fetch_release_assets",
            lambda version, *, repo: [
                tui_release.ReleaseAsset(name="macos-dmg", download_url="https://x/macos-dmg"),
            ],
        )
        seen: dict = {}

        def _fake_download(url, dest_dir, **kw):
            seen["dest_dir_type"] = type(dest_dir)
            assert isinstance(dest_dir, Path)
            return dest_dir / "macos-dmg"

        monkeypatch.setattr(tui_release, "download_asset", _fake_download)

        config = Config(
            repos=[Repo(name="vimcode", github="acme/vimcode", develop_branch=None)],
            machines=[_machine("m1", caps=["python"])],
        )
        workdir = tmp_path / "work"
        plan = _plan(source="download", ref="0.15.0")
        obtained = _default_download_artifact(plan, config=config, workdir=str(workdir))
        assert issubclass(seen["dest_dir_type"], Path)
        assert obtained.binary_path == str(workdir / "macos-dmg")


# ── _resolve_bugbash_lane: resolved via discover_lanes, not hand-built ─────


class TestResolveBugbashLane:
    def test_disambiguates_sibling_routes_sharing_a_kind(self) -> None:
        """#3660 review round 1 — the exact #3615 case: two routes with the
        SAME `kind` but different `label:` must resolve to distinct
        per-route platform labels (and carry their own setup/launch
        command), not the bare driver_kind both would share if hand-built.
        """
        gui_route = AcceptanceDriverConfig(
            kind="win-native", capability="windows", label="gui",
            setup="cargo xwin build --bin vimcode", run="vimcode.exe",
        )
        terminal_route = AcceptanceDriverConfig(
            kind="win-native", capability="windows", label="terminal",
            run="vimcode-term.exe",
        )
        config = Config(
            repos=[Repo(name="vimcode", github="acme/vimcode")],
            machines=[_machine("dell64", caps=["windows"])],
            acceptance=AcceptanceConfig(drivers={
                "vimcode": AcceptanceDriverConfig(routes=[gui_route, terminal_route]),
            }),
        )
        client = _FakeHealthClient({})

        gui_lane, gui_fallback = _resolve_bugbash_lane(
            config, "vimcode", gui_route, "dell64", http_client=client,
        )
        assert gui_fallback == ""
        assert gui_lane.platform == "win-native:gui"
        assert gui_lane.setup == "cargo xwin build --bin vimcode"
        assert gui_lane.launch_command == "vimcode.exe"

        terminal_lane, terminal_fallback = _resolve_bugbash_lane(
            config, "vimcode", terminal_route, "dell64", http_client=client,
        )
        assert terminal_fallback == ""
        assert terminal_lane.platform == "win-native:terminal"
        assert terminal_lane.launch_command == "vimcode-term.exe"

    def test_a_host_other_than_discover_lanes_own_pick_still_matches_the_route(
        self,
    ) -> None:
        """#3660 review round 2: the match must be keyed on the ROUTE, not
        on the machine. `discover_lanes` picks the FIRST configured machine
        claiming the capability (no pause/cordon filter), while
        `pick_nightly_host` ranks idle-first and filters cordoned/paused
        hosts — so the two routinely disagree, and keying on
        `lane.machine == machine_name` silently fell through to the
        hand-built lane whose bare `driver_kind` platform label IS the
        #3615 dedupe collision."""
        gui_route = AcceptanceDriverConfig(
            kind="win-native", capability="windows", label="gui",
            setup="cargo xwin build", run="vimcode.exe",
        )
        config = Config(
            repos=[Repo(name="vimcode", github="acme/vimcode")],
            # `_pick_lane_machine` will choose `dell64` (first configured);
            # the nightly host picker chose the OTHER windows box.
            machines=[
                _machine("dell64", caps=["windows"]),
                _machine("spare64", caps=["windows"]),
            ],
            acceptance=AcceptanceConfig(drivers={
                "vimcode": AcceptanceDriverConfig(routes=[gui_route]),
            }),
        )
        lane, fallback = _resolve_bugbash_lane(
            config, "vimcode", gui_route, "spare64", http_client=_FakeHealthClient({}),
        )
        assert fallback == ""  # no silent fallback
        assert lane.platform == "win-native:gui"  # not the bare "win-native"
        assert lane.machine == "spare64"  # the host that actually ran it
        assert lane.launch_command == "vimcode.exe"

    def test_falls_back_to_a_hand_built_lane_for_a_non_bugbash_driver_kind(self) -> None:
        """`cli-pytest` isn't one of `coord.bugbash.LANE_DRIVER_KINDS` —
        `discover_lanes` resolves nothing for it, so this must still
        return a usable lane rather than crashing — and must SAY it fell
        back (#3660 review round 2) rather than doing it silently."""
        config = _config(kind="cli-pytest", capability="python")
        driver_cfg = config.acceptance.drivers["vimcode"]
        lane, fallback = _resolve_bugbash_lane(config, "vimcode", driver_cfg, "m1")
        assert lane.platform == "cli-pytest"
        assert lane.driver_kind == "cli-pytest"
        assert lane.machine == "m1"
        assert "hand-built lane" in fallback
        assert "cli-pytest" in fallback
