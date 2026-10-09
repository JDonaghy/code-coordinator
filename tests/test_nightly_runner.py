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

from pathlib import Path

import pytest

from coord.acceptance_drivers import DriverResult
from coord.config import AcceptanceConfig, AcceptanceDriverConfig, Config, SmokeTestsConfig
from coord.models import Board, Machine, Repo
from coord.nightly_runner import (
    NightlyRunnerError,
    ObtainedArtifact,
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
        app red — never files an issue, never records a result."""
        monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))
        config = _config(kind="mac-native", capability="macos", caps=["macos"])
        client = _FakeHealthClient({"m1.tail": _gui_lane_health(crit=True)})
        runner = _FakeRunner()
        report = run_nightly_smoke(
            repo="vimcode", artifact="macos-dmg", spec="", config=config, board=Board(),
            dry_run=False, http_client=client, runner=runner,
        )
        assert report.ran is False
        assert report.infra_blocked is True
        assert runner.calls == []
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
