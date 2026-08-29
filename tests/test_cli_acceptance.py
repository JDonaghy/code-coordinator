"""CLI tests for `coord acceptance run` / `coord acceptance record` (#944)."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.models import Proposal


CONFIG_YAML = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
    repo_paths:
      coord-tui: {repo_path}
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: {run_cmd}
{entrypoint}"""


def _write_config(
    tmp_path: Path, *, repo_path: str, run_cmd: str, entrypoint: str = "",
) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML.format(
        repo_path=repo_path,
        run_cmd=json.dumps(run_cmd),
        # #1552: the driver's crate-root entry point, omitted entirely by
        # default so every pre-existing fixture keeps exercising the
        # no-entrypoint (directory-discovered) shape.
        entrypoint=f"      entrypoint: {json.dumps(entrypoint)}\n" if entrypoint else "",
    ))
    return p


def _write_manifest(acceptance_root: Path, mapping: dict[str, int]) -> None:
    ms = acceptance_root / "ms01"
    ms.mkdir(parents=True, exist_ok=True)
    tests_yaml = "\n".join(f"  {k}: {v}" for k, v in mapping.items())
    (ms / "manifest.yml").write_text(f"tests:\n{tests_yaml}\n")


class TestAcceptanceRun:
    def test_run_scoped_to_issue_all_pass(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "pass"},
            {"id": "ms01::b", "status": "pass"},
        ]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(cwd / "tests" / "acceptance", {"ms01::a": 944, "ms01::b": 944})
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["issue"] == 944
        assert payload["total"] == 2
        assert payload["green"] is True

    def test_run_reports_failure_and_nonzero_exit(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "pass"},
            {"id": "ms01::b", "status": "fail", "message": "expected A got B"},
        ]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(cwd / "tests" / "acceptance", {"ms01::a": 944, "ms01::b": 944})
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["failed"] == 1
        assert payload["green"] is False

    def test_run_filters_out_other_issues_tests(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "pass"},
            {"id": "ms01::other", "status": "fail"},
        ]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(
            cwd / "tests" / "acceptance", {"ms01::a": 944, "ms01::other": 945},
        )
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # Only ms01::a belongs to #944 — #945's failure must not leak in.
        assert payload["total"] == 1
        assert payload["green"] is True

    def test_run_all_ignores_manifest(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        # No manifest at all — --all must still work.
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["scope"] == "all"
        assert payload["total"] == 1

    def test_run_requires_issue_or_all(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd="echo '{}'")
        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "--issue N or --all" in result.output

    def test_run_missing_driver_errors(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: coord-tui\n    github: acme/coord-tui\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [coord-tui]\n"
        )
        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all", "--config", str(p),
        ])
        assert result.exit_code == 1
        assert "no acceptance driver configured" in result.output

    def test_run_missing_manifest_errors(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd="echo '{}'")
        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "not been authored" in result.output

    def test_run_issue_with_no_slice_errors(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(cwd / "tests" / "acceptance", {"ms01::a": 1})
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd="echo '{}'")
        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "no acceptance slice" in result.output

    # ── #1552: name a wiring failure instead of a bare green=false ──────────

    def test_run_names_unwired_slice_when_no_manifest_ids_ran(
        self, tmp_path: Path
    ) -> None:
        """A slice authored but never registered in the driver's entry point
        compiles into nothing and emits no verdicts at all. `total=0,
        green=false` sends the next round hunting a test failure that never
        ran — say what actually happened, and name the entry point."""
        blob = json.dumps({"tests": [{"id": "ms01::unrelated", "status": "pass"}]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        # #2896: an entrypoint-linked driver's manifests live beside the
        # entrypoint (tui/tests/acceptance.rs -> tui/tests/acceptance/), not
        # the repo-root tests/acceptance/ — see acceptance_root_for_driver.
        _write_manifest(
            cwd / "tui" / "tests" / "acceptance",
            {"ms01::a": 944, "ms01::b": 944, "ms01::unrelated": 900},
        )
        config_path = _write_config(
            tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'",
            entrypoint="tui/tests/acceptance.rs",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "none of which appeared in the driver output" in result.output.lower()
        assert "wiring failure, not a test failure" in result.output
        assert "tui/tests/acceptance.rs" in result.output
        # Both missing ids are named so the author knows exactly what to wire.
        assert "ms01::a" in result.output
        assert "ms01::b" in result.output

    def test_run_flags_partially_missing_ids_without_changing_the_verdict(
        self, tmp_path: Path
    ) -> None:
        """A partial miss (one of two slice files wired) still reports the
        driver's own verdict — but the vanished id is named rather than
        silently dropped from the denominator."""
        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        # #2896: see the sibling test above — entrypoint-linked manifests
        # live under tui/tests/acceptance/, not the repo-root tests/acceptance/.
        _write_manifest(
            cwd / "tui" / "tests" / "acceptance", {"ms01::a": 944, "ms01::b": 944},
        )
        config_path = _write_config(
            tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'",
            entrypoint="tui/tests/acceptance.rs",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert "1 of issue #944's 2 manifest test-id(s) did not appear" in result.output
        assert "ms01::b" in result.output
        payload = json.loads(result.output[result.output.index("{"):])
        assert payload["missing_ids"] == ["ms01::b"]
        assert payload["total"] == 1
        assert payload["green"] is True

    def test_run_says_nothing_extra_when_every_id_ran(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(cwd / "tests" / "acceptance", {"ms01::a": 944})
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "did not appear" not in result.output
        payload = json.loads(result.output)
        assert "missing_ids" not in payload
        assert "reason" not in payload

    def test_run_unwired_message_degrades_without_an_entrypoint(
        self, tmp_path: Path
    ) -> None:
        """The pytest route declares no entry point — still name the failure,
        just without a registration hint that wouldn't apply."""
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(cwd / "tests" / "acceptance", {"ms01::a": 944})
        config_path = _write_config(
            tmp_path, repo_path=str(cwd), run_cmd="echo '{\"tests\": []}'",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "wiring failure, not a test failure" in result.output
        assert "not being discovered by the driver's run command" in result.output
        assert "entrypoint" not in result.output


def _write_manifest_with_expected_red(
    acceptance_root: Path, tests: dict[str, int], expected_red: dict[int, list[str]],
) -> None:
    ms = acceptance_root / "ms01"
    ms.mkdir(parents=True, exist_ok=True)
    lines = ["tests:"]
    lines += [f"  {k}: {v}" for k, v in tests.items()]
    lines.append("expected_red:")
    for issue, ids in expected_red.items():
        lines.append(f"  {issue}:")
        lines += [f"    - {i}" for i in ids]
    (ms / "manifest.yml").write_text("\n".join(lines) + "\n")


class TestAcceptanceRunCI:
    """#2164: `coord acceptance run --all --ci` — the CI wrapper that
    honours `expected_red:` so a sealed slice authored red can merge
    without turning the default branch red."""

    def test_ci_requires_all(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd="echo '{}'")
        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944", "--ci",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "--ci requires --all" in result.output

    def test_expected_red_failure_does_not_block_ci(self, tmp_path: Path) -> None:
        """Acceptance criterion 1: a sealed slice authored red merges
        without turning the default branch red."""
        blob = json.dumps({"tests": [
            {"id": "wide_label", "status": "fail", "message": "glyph dropped"},
            {"id": "ascii_label_is_unchanged", "status": "pass"},
        ]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest_with_expected_red(
            cwd / "tests" / "acceptance",
            {"wide_label": 554, "ascii_label_is_unchanged": 554},
            {554: ["wide_label"]},
        )
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all", "--ci",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "not a CI failure" in result.output

    def test_expected_red_that_passes_hard_fails_ci(self, tmp_path: Path) -> None:
        """Acceptance criterion 2: an expected_red entry that passes fails
        the run, loudly and distinguishably from an ordinary failure."""
        blob = json.dumps({"tests": [{"id": "wide_label", "status": "pass"}]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest_with_expected_red(
            cwd / "tests" / "acceptance", {"wide_label": 554}, {554: ["wide_label"]},
        )
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all", "--ci",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "HARD FAILURE" in result.output
        assert "wide_label" in result.output

    def test_real_failure_still_blocks_ci(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [
            {"id": "wide_label", "status": "fail"},
            {"id": "unrelated_regression", "status": "fail"},
        ]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest_with_expected_red(
            cwd / "tests" / "acceptance", {"wide_label": 554}, {554: ["wide_label"]},
        )
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all", "--ci",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1

    def test_no_expected_red_behaves_like_plain_all(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [{"id": "a", "status": "pass"}]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all", "--ci",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ci_green"] is True


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
    )


def _init_repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """A local bare "origin" plus a clone-shaped work checkout tracking it —
    enough to exercise #2038's behind-origin check without any real
    network."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))

    work = tmp_path / "repo"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("hello\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-u", "origin", "main")
    return origin, work


class TestAcceptanceRunCheckoutFreshness:
    """#2038: `coord acceptance run` silently drove whatever the current
    checkout happened to be, with no indication of which commit — a stale
    tree read as a catastrophic sealed-suite failure. `run` now prints the
    SHA/branch it's testing and warns (stderr, never a gate) when the tree
    is dirty or behind `origin/<default_branch>`."""

    def test_run_prints_sha_and_branch_header(self, tmp_path: Path) -> None:
        _origin, work = _init_repo_with_origin(tmp_path)
        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        _write_manifest(work / "tests" / "acceptance", {"ms01::a": 944})
        # Commit the manifest too — an untracked manifest would itself read
        # as a dirty tree, muddying this "clean" baseline case.
        _git(work, "add", "tests")
        _git(work, "commit", "-m", "manifest")
        config_path = _write_config(tmp_path, repo_path=str(work), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(work), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "acceptance: coord-tui @ " in result.output
        assert "(main)" in result.output
        assert "uncommitted changes" not in result.output
        assert "behind" not in result.output
        # The verdict JSON must still be the trailing, cleanly-parseable blob.
        payload = json.loads(result.output[result.output.index("{"):])
        assert payload["green"] is True

    def test_run_warns_on_dirty_working_tree(self, tmp_path: Path) -> None:
        _origin, work = _init_repo_with_origin(tmp_path)
        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        _write_manifest(work / "tests" / "acceptance", {"ms01::a": 944})
        config_path = _write_config(tmp_path, repo_path=str(work), run_cmd=f"echo '{blob}'")

        # Uncommitted change — must not exist as a driver-invisible surprise.
        (work / "README.md").write_text("modified\n")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(work), "--config", str(config_path),
        ])
        assert "uncommitted changes" in result.output

    def test_run_warns_when_behind_origin_default_branch(self, tmp_path: Path) -> None:
        origin, work = _init_repo_with_origin(tmp_path)
        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        _write_manifest(work / "tests" / "acceptance", {"ms01::a": 944})
        config_path = _write_config(tmp_path, repo_path=str(work), run_cmd=f"echo '{blob}'")

        # A second clone pushes a commit `work` never fetches — mirrors the
        # #2038 incident: `work`'s checkout is now one commit stale.
        other = tmp_path / "other"
        _git(tmp_path, "clone", str(origin), str(other))
        _git(other, "config", "user.email", "test@example.com")
        _git(other, "config", "user.name", "Test")
        (other / "NEWFILE.txt").write_text("new\n")
        _git(other, "add", "NEWFILE.txt")
        _git(other, "commit", "-m", "second commit")
        _git(other, "push", "origin", "main")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(work), "--config", str(config_path),
        ])
        assert "checkout is 1 commit behind origin/main" in result.output
        assert "git pull" in result.output
        # Warning only — never a gate; the driver's own verdict still decides
        # exit code (all tests passed here).
        assert result.exit_code == 0, result.output

    def test_run_over_non_git_checkout_prints_no_freshness_header(
        self, tmp_path: Path
    ) -> None:
        """A plain (non-git) directory — same shape a throwaway/extracted
        checkout might be — degrades to no header at all rather than
        crashing the run."""
        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(cwd / "tests" / "acceptance", {"ms01::a": 944})
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "acceptance:" not in result.output
        payload = json.loads(result.output)
        assert payload["green"] is True


ROUTED_CONFIG_YAML = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
    repo_paths:
      coord-tui: {repo_path}
acceptance:
  drivers:
    coord-tui:
      routes:
        - match: "coord/**"
          kind: cli-pytest
          run: "pytest tests/acceptance/{{ms}}"
"""


def _write_routed_config(tmp_path: Path, *, repo_path: str) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(ROUTED_CONFIG_YAML.format(repo_path=repo_path))
    return p


class TestAcceptanceRunRouted:
    """#1125 review findings 1/2: `coord acceptance run` against a routed
    repo (acceptance.drivers.<repo>.routes) must (a) require --for-path to
    resolve a route rather than falling back to the generic "not
    configured" error, and (b) substitute the `{ms}` template from the
    issue's manifest-mapped ms-NN dir before running."""

    def test_run_without_for_path_errors_actionably(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        config_path = _write_routed_config(tmp_path, repo_path=str(cwd))

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "no route matched" in result.output
        assert "--for-path" in result.output

    def test_run_substitutes_ms_template_from_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        ms_dir = cwd / "tests" / "acceptance" / "ms-37"
        ms_dir.mkdir(parents=True)
        (ms_dir / "manifest.yml").write_text("tests:\n  ms-37::a: 944\n")
        config_path = _write_routed_config(tmp_path, repo_path=str(cwd))

        captured = {}

        def fake_run_driver(kind, run_command, cwd, **kwargs):
            captured["kind"] = kind
            captured["run_command"] = run_command
            captured["ms"] = kwargs.get("ms")
            from coord.acceptance_drivers import DriverResult
            return DriverResult(exit_code=0, tests=[{"id": "ms-37::a", "status": "pass"}])

        monkeypatch.setattr("coord.commands.acceptance.run_driver", fake_run_driver)

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--for-path", "coord/acceptance.py",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert captured["kind"] == "cli-pytest"
        assert captured["ms"] == "ms-37"

    def test_run_wires_driver_setup_into_run_driver(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1733: `AcceptanceDriverConfig.setup` (e.g. `npm ci` for
        web-playwright) must actually reach `run_driver`'s `setup_command`
        — otherwise the config field is parsed but never consulted, the
        exact "declared but ignored" shape #1733 reports for capability
        routing before #966 fixed it."""
        cwd = tmp_path / "repo"
        cwd.mkdir()
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(f"""\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
    repo_paths:
      coord-tui: {cwd}
acceptance:
  drivers:
    coord-tui:
      routes:
        - match: "coord/dashboard/webapp/**"
          kind: web-playwright
          run: "npx playwright test tests/acceptance/{{ms}}"
          setup: "npm ci"
""")

        captured = {}

        def fake_run_driver(kind, run_command, cwd, **kwargs):
            captured["setup_command"] = kwargs.get("setup_command")
            from coord.acceptance_drivers import DriverResult
            return DriverResult(exit_code=0, tests=[{"id": "a", "status": "pass"}])

        monkeypatch.setattr("coord.commands.acceptance.run_driver", fake_run_driver)

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all",
            "--for-path", "coord/dashboard/webapp/app.ts",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert captured["setup_command"] == "npm ci"


def _init_git_repo(
    path: Path, *, manifest: dict[str, int] | None = None, manifest_ms_dir: str = "ms01",
) -> str:
    """Create a minimal git repo (with a real "origin" remote — a bare repo
    alongside it) and one commit pushed to origin; returns the commit SHA.

    ``coord acceptance record`` always does a real ``git fetch origin``
    before checking out the worktree, so the test repo needs an actual
    origin remote, not just local history.

    When *manifest* is given, ``tests/acceptance/<manifest_ms_dir>/manifest.
    yml`` mapping each test id to its issue number is committed too —
    ``record`` checks out this exact SHA in a throwaway worktree, so the
    manifest must be part of history at the SHA being recorded, not just
    sitting in the base checkout's working tree.
    """
    bare = path.parent / f"{path.name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    if manifest:
        ms = path / "tests" / "acceptance" / manifest_ms_dir
        ms.mkdir(parents=True, exist_ok=True)
        tests_yaml = "\n".join(f"  {k}: {v}" for k, v in manifest.items())
        (ms / "manifest.yml").write_text(f"tests:\n{tests_yaml}\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "push", "-q", "origin", branch], cwd=path, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    return sha


@pytest.fixture(autouse=True)
def _acceptance_worktrees_in_tmp(monkeypatch, tmp_path: Path):
    """Keep `coord acceptance record`'s throwaway worktree under tmp_path —
    the real implementation lives under ``~/.coord/acceptance-worktrees/``
    (outside the base checkout, mirroring `coord test`'s worktree), which
    must never leak into the real user's home directory during tests."""
    wt_root = tmp_path / "acceptance-worktrees"

    def _fake_path(repo_name: str, issue_number: int) -> Path:
        return wt_root / f"{repo_name}-{issue_number}"

    monkeypatch.setattr(
        "coord.commands.acceptance._acceptance_worktree_path", _fake_path,
    )


def _acceptance_row(coord_db, assignment_id: str) -> dict:
    row = coord_db.execute(
        "SELECT acceptance_state, acceptance_reason, acceptance_sha "
        "FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    assert row is not None, f"no assignment row for {assignment_id!r}"
    return dict(row)


class TestAcceptanceRecord:
    def test_record_passed_writes_board_verdict(self, tmp_path: Path, coord_db) -> None:
        from coord import state

        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms01::a": 944})

        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        state.record_dispatched(
            assignment_id="aid-1",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "Acceptance PASSED" in result.output

        row = _acceptance_row(coord_db, "aid-1")
        assert row["acceptance_state"] == "passed"
        assert row["acceptance_sha"] == sha

    def test_record_failed_writes_board_verdict_and_context(
        self, tmp_path: Path, coord_db,
    ) -> None:
        from coord import state

        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms01::a": 944})

        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "fail", "message": "expected A got B"},
        ]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        state.record_dispatched(
            assignment_id="aid-2",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "Acceptance FAILED" in result.output

        row = _acceptance_row(coord_db, "aid-2")
        assert row["acceptance_state"] == "failed"
        assert "expected A got B" in (row["acceptance_reason"] or "")

        # #603: a failure is recorded as durable per-issue context too.
        entries = state.list_issue_context("coord-tui", 944)
        assert any("Acceptance FAILED" in e["body"] for e in entries)

    def test_record_routed_driver_without_for_path_errors_actionably(
        self, tmp_path: Path, coord_db,
    ) -> None:
        """#1125 review findings 1/2: `coord acceptance record` against a
        routed repo needs --for-path to resolve a driver at all."""
        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms-37::a": 944}, manifest_ms_dir="ms-37")
        config_path = _write_routed_config(tmp_path, repo_path=str(repo_dir))

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "no route matched" in result.output
        assert "--for-path" in result.output

    def test_record_routed_driver_substitutes_ms_and_writes_verdict(
        self, tmp_path: Path, coord_db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1125 review findings 1/2: with --for-path resolving the route,
        record must also substitute `{ms}` from the checked-out worktree's
        manifest before running the driver."""
        from coord import state

        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms-37::a": 944}, manifest_ms_dir="ms-37")
        config_path = _write_routed_config(tmp_path, repo_path=str(repo_dir))

        state.record_dispatched(
            assignment_id="aid-routed",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        captured = {}

        def fake_run_driver(kind, run_command, cwd, **kwargs):
            captured["kind"] = kind
            captured["ms"] = kwargs.get("ms")
            from coord.acceptance_drivers import DriverResult
            return DriverResult(exit_code=0, tests=[{"id": "ms-37::a", "status": "pass"}])

        monkeypatch.setattr("coord.commands.acceptance.run_driver", fake_run_driver)

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--for-path", "coord/acceptance.py",
            "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert captured["kind"] == "cli-pytest"
        assert captured["ms"] == "ms-37"

        row = _acceptance_row(coord_db, "aid-routed")
        assert row["acceptance_state"] == "passed"

    def test_record_runs_setup_before_run_in_the_throwaway_worktree(
        self, tmp_path: Path, coord_db,
    ) -> None:
        """#1733 direct regression: `acceptance record`'s throwaway `git
        worktree add --detach` checkout is dependency-less (`node_modules`
        is gitignored, never checked out) — a JS driver's `run` alone fails
        with a bare `exit 127` there. `setup` must actually run, IN that
        worktree, BEFORE `run` — not just be parsed and ignored (the #1733
        report's root cause). `run` here only succeeds if it finds a marker
        file `setup` created in the same (worktree) cwd, so this proves
        ordering + cwd, not merely that both commands happened to run
        somewhere.
        """
        from coord import state

        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms01::a": 944})

        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        run_cmd = (
            f"test -f provisioned && echo '{blob}' "
            "|| (echo 'MISSING MARKER — setup did not run first' >&2 && exit 1)"
        )
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(f"""\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
    repo_paths:
      coord-tui: {repo_dir}
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: {json.dumps(run_cmd)}
      setup: "touch provisioned"
""")

        state.record_dispatched(
            assignment_id="aid-setup",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "Acceptance PASSED" in result.output

        row = _acceptance_row(coord_db, "aid-setup")
        assert row["acceptance_state"] == "passed"

    def test_record_routed_setup_runs_before_run_in_the_throwaway_worktree(
        self, tmp_path: Path, coord_db,
    ) -> None:
        """#1817 direct regression: the bug report was specifically a
        *routed* driver (`coord/dashboard/webapp/**`, resolved via
        `--for-path`, exactly like the real `web-playwright` route) whose
        `setup:` never reached `record`'s real throwaway worktree in
        production, even though the mechanism itself worked. Neither
        existing test covered this combination:
        `test_record_runs_setup_before_run_in_the_throwaway_worktree` above
        proves a *flat* driver's setup reaches record's real worktree;
        `test_run_wires_driver_setup_into_run_driver` proves a *routed*
        driver's setup reaches a *mocked* `run_driver` for `acceptance run`.
        This closes the gap so routed+record can't silently regress to the
        "declared but ignored" shape #1817 reports.
        """
        from coord import state

        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms01::a": 944})

        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        run_cmd = (
            f"test -f provisioned && echo '{blob}' "
            "|| (echo 'MISSING MARKER — setup did not run first' >&2 && exit 1)"
        )
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(f"""\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
    repo_paths:
      coord-tui: {repo_dir}
acceptance:
  drivers:
    coord-tui:
      routes:
        - match: "coord/dashboard/webapp/**"
          kind: tui-tuidriver
          run: {json.dumps(run_cmd)}
          setup: "touch provisioned"
""")

        state.record_dispatched(
            assignment_id="aid-routed-setup",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--for-path", "coord/dashboard/webapp/app.tsx",
            "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "Acceptance PASSED" in result.output

        row = _acceptance_row(coord_db, "aid-routed-setup")
        assert row["acceptance_state"] == "passed"

    def test_record_no_work_assignment_errors(self, tmp_path: Path, coord_db) -> None:
        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms01::a": 944})

        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "no work assignment found" in result.output

        # #944 review: this is a lookup error (no `work` assignment for the
        # repo/issue), not a real failing-verdict "kept for inspection" case
        # — the throwaway worktree must not be left behind.
        wt_path = tmp_path / "acceptance-worktrees" / "coord-tui-944"
        assert not wt_path.exists(), "worktree leaked on no-work-assignment error path"

    def test_record_manifest_missing_cleans_up_worktree(
        self, tmp_path: Path, coord_db,
    ) -> None:
        # No manifest committed at all — `_scoped_verdict` exit(1)s inside
        # `dump_manifest_error_hint` before a work-assignment lookup ever
        # happens; that path must clean up the worktree too (#944 review).
        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest=None)

        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "not been authored" in result.output

        wt_path = tmp_path / "acceptance-worktrees" / "coord-tui-944"
        assert not wt_path.exists(), "worktree leaked on manifest-missing error path"

    def test_concurrent_record_calls_for_same_issue_are_serialized(
        self, tmp_path: Path, coord_db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#2352 direct regression: two concurrent `coord acceptance record`
        calls for the SAME (repo, issue) — e.g. an orphaned drive process
        (#1660) racing a fresh queue relaunch, or a by-hand re-run
        overlapping an in-flight one — must never interleave their
        worktree-prep + driver run. Before the fix, `_acceptance_record_local`
        did an unconditional `git worktree remove --force` + `add --force`
        on the ONE path this (repo, issue) reuses across every SHA/round,
        with no lock guarding it: a second invocation's remove could rip
        the worktree out from under the first's in-flight build, producing
        a corrupted build that looks exactly like a genuine red suite (a
        false `acceptance_state=failed`) instead of raising anything
        distinguishable.

        This drives two threads through `_acceptance_record_local` for the
        same repo/issue/sha concurrently, with `run_driver` slowed down and
        instrumented, and asserts at most one thread is ever inside the
        locked region (worktree-prep through verdict write) at a time.
        """
        import sqlite3

        from coord import db, state
        from coord.acceptance_drivers import DriverResult
        from coord.commands.acceptance import _acceptance_record_local
        from coord.db import _ensure_schema

        # #2352 test-only wrinkle: the autouse `coord_db` fixture's
        # in-memory connection is opened with sqlite3's default
        # `check_same_thread=True`, which raises the instant a background
        # thread touches it — unrelated to the race this test is actually
        # driving at. Production's real singleton (`coord.db._open`)
        # already opens with `check_same_thread=False` (the coordinator
        # legitimately writes from more than one thread), so swapping in a
        # thread-safe in-memory connection here just matches production
        # instead of tripping over a fixture-only restriction. Use `conn`
        # (not the `coord_db` fixture argument, which still points at the
        # now-orphaned original connection) for any DB reads below.
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        db.override_connection(conn)

        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms01::a": 944})
        config_path = _write_config(
            tmp_path, repo_path=str(repo_dir), run_cmd="echo unused",
        )

        state.record_dispatched(
            assignment_id="aid-race",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def _slow_driver(kind, run_command, cwd, ms=None, setup_command=None):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                # Wide enough that, without the #2352 lock, the second
                # thread's (fast) git worktree remove/add would complete
                # and reach this same function while the first thread is
                # still "mid-build" here — reproducing the race window
                # that corrupted the real cargo build in the incident.
                time.sleep(0.3)
                return DriverResult(
                    exit_code=0, tests=[{"id": "ms01::a", "status": "pass"}],
                )
            finally:
                with state_lock:
                    active -= 1

        monkeypatch.setattr("coord.commands.acceptance.run_driver", _slow_driver)

        errors: list[BaseException] = []

        def _run() -> None:
            try:
                _acceptance_record_local("coord-tui", 944, sha, config_path)
            except BaseException as e:  # noqa: BLE001 - captured for assertion
                errors.append(e)

        threads = [threading.Thread(target=_run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not any(t.is_alive() for t in threads), "record call hung"
        assert not errors, f"_acceptance_record_local raised: {errors!r}"
        assert max_active == 1, (
            "both threads ran the driver concurrently — the #2352 lock did "
            "not serialize concurrent `coord acceptance record` calls for "
            "the same (repo, issue)"
        )

        row = _acceptance_row(conn, "aid-race")
        assert row["acceptance_state"] == "passed"


def _init_git_repo_with_expected_red(
    path: Path, *, tests: dict[str, int], expected_red: dict[int, list[str]],
) -> tuple[Path, str]:
    """#2164: like `_init_git_repo`, but seeded with an `expected_red:`
    block too. Returns (bare origin path, commit sha)."""
    bare = path.parent / f"{path.name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    _write_manifest_with_expected_red(
        path / "tests" / "acceptance", tests, expected_red,
    )
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "push", "-q", "origin", branch], cwd=path, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    return bare, sha


class TestAcceptanceRecordDoesNotClearExpectedRed:
    """#2164 review (blocking finding 1): clearing `expected_red` from
    inside `coord acceptance record` was the bug — it could run before
    Test/Review/the actual merge to the default branch, reopening the
    exact "red default branch" failure #2164 exists to prevent. `record`
    now only ever writes the verdict to the board; clearing moved to
    `coord.merge_queue.process`'s post-merge hook (see
    tests/test_merge_queue.py's `TestExpectedRedClearOnMerge`) and is
    exercised there, not here — `record` must not touch git at all beyond
    its own read-only worktree checkout."""

    def test_green_record_does_not_push_or_mutate_origin(
        self, tmp_path: Path, coord_db,
    ) -> None:
        from coord import state

        repo_dir = tmp_path / "repo"
        bare, sha = _init_git_repo_with_expected_red(
            repo_dir,
            tests={"ms01::a": 944, "ms01::b": 944},
            expected_red={944: ["ms01::a"]},
        )
        before = subprocess.run(
            ["git", "--git-dir", str(bare), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "pass"},
            {"id": "ms01::b", "status": "pass"},
        ]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        state.record_dispatched(
            assignment_id="aid-clear-1",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "cleared expected_red" not in result.output
        # #2164 review: no direct push to origin — that's the raw-push bug
        # a protected default branch would reject outright.
        assert "expected-red" in result.output  # points at the new listing command

        after = subprocess.run(
            ["git", "--git-dir", str(bare), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert after == before, "record must never mutate origin"

    def test_no_expected_red_entries_prints_no_note(
        self, tmp_path: Path, coord_db,
    ) -> None:
        """The overwhelmingly common case — a slice with nothing
        expected-red — gets no expected_red note at all."""
        from coord import state

        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms01::a": 944})

        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        state.record_dispatched(
            assignment_id="aid-clear-3",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "cleared expected_red" not in result.output


class TestAcceptanceExpectedRedCommand:
    """#2164 acceptance criterion 4: `expected_red` entries are visible
    wherever gate state is read. `coord acceptance expected-red` is that
    surface — API-only, no local checkout required."""

    def test_lists_entries_and_flags_a_closed_issue_as_stuck(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true")

        with patch(
            "coord.commands.acceptance.list_expected_red_via_api",
            return_value={"ms01": {944: {"ms01::a"}, 945: {"ms01::z"}}},
        ), patch("coord.commands.acceptance.github_ops") as mock_gh:
            mock_gh.get_issue.side_effect = lambda repo, n: (
                {"state": "CLOSED"} if n == 944 else {"state": "OPEN"}
            )
            result = CliRunner().invoke(main, [
                "acceptance", "expected-red", "coord-tui", "--config", str(config_path),
            ])

        assert result.exit_code == 0, result.output
        assert "ms01:" in result.output
        assert "#944: ms01::a" in result.output
        assert "STUCK" in result.output.split("#944")[1].split("#945")[0]
        assert "#945: ms01::z" in result.output
        assert "STUCK" not in result.output.split("#945")[1]

    def test_no_entries_reports_clean(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true")

        with patch(
            "coord.commands.acceptance.list_expected_red_via_api", return_value={},
        ):
            result = CliRunner().invoke(main, [
                "acceptance", "expected-red", "coord-tui", "--config", str(config_path),
            ])

        assert result.exit_code == 0, result.output
        assert "no expected_red entries" in result.output

    def test_unknown_repo_errors(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true")
        result = CliRunner().invoke(main, [
            "acceptance", "expected-red", "not-a-repo", "--config", str(config_path),
        ])
        assert result.exit_code == 2
        assert "unknown repo" in result.output

    def test_sweeps_the_relocated_root_for_an_entrypoint_linked_driver(
        self, tmp_path: Path,
    ) -> None:
        """#2896 review (blocking, impact 2): this listing hardcoded the
        repo-root `tests/acceptance/`, so every relocated milestone's
        entries were silently omitted from it — the exact "long-lived
        expected_red entry is invisible debt" failure #2164 built this
        command to surface."""
        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true",
            entrypoint="tui/tests/acceptance.rs",
        )

        with patch(
            "coord.commands.acceptance.list_expected_red_via_api", return_value={},
        ) as mock_list:
            result = CliRunner().invoke(main, [
                "acceptance", "expected-red", "coord-tui", "--config", str(config_path),
            ])

        assert result.exit_code == 0, result.output
        assert mock_list.call_args.kwargs["search_roots"] == [
            "tests/acceptance/", "tui/tests/acceptance/",
        ]

    def test_directory_discovered_driver_still_sweeps_only_the_shared_root(
        self, tmp_path: Path,
    ) -> None:
        """The ms-37 shape (no `entrypoint:`) never moved — adding roots
        must not change what a directory-discovered driver searches."""
        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true",
        )

        with patch(
            "coord.commands.acceptance.list_expected_red_via_api", return_value={},
        ) as mock_list:
            result = CliRunner().invoke(main, [
                "acceptance", "expected-red", "coord-tui", "--config", str(config_path),
            ])

        assert result.exit_code == 0, result.output
        assert mock_list.call_args.kwargs["search_roots"] == ["tests/acceptance/"]


class TestAcceptanceExpectedRedClear:
    """#2266: the remedy half of the #2164 detector — `--clear` invokes
    `clear_expected_red_via_pr` for every STUCK entry `expected-red`
    already finds, and never for an entry whose issue is legitimately
    still open."""

    @staticmethod
    def _invoke(config_path: Path, *extra_args: str):
        return CliRunner().invoke(main, [
            "acceptance", "expected-red", "coord-tui", "--config", str(config_path),
            "--clear", *extra_args,
        ])

    def test_clear_acts_only_on_the_stuck_closed_issue(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true")

        with patch(
            "coord.commands.acceptance.list_expected_red_via_api",
            return_value={"ms01": {944: {"ms01::a"}, 945: {"ms01::z"}}},
        ), patch("coord.commands.acceptance.github_ops") as mock_gh, patch(
            "coord.acceptance.clear_expected_red_via_pr",
        ) as mock_clear:
            mock_gh.get_issue.side_effect = lambda repo, n: (
                {"state": "CLOSED"} if n == 944 else {"state": "OPEN"}
            )
            mock_clear.return_value = "cleared expected_red for #944: ms01::a (PR #501)"

            result = self._invoke(config_path)

        assert result.exit_code == 0, result.output
        mock_clear.assert_called_once()
        args, _kwargs = mock_clear.call_args
        assert args[3] == 944  # issue_number — never called for the open #945
        assert "clearing 1 STUCK issue(s)" in result.output
        assert "#944: cleared expected_red for #944" in result.output
        assert "#945" not in result.output.split("clearing 1 STUCK issue(s)")[1]

    def test_clear_forwards_the_relocated_search_roots(self, tmp_path: Path) -> None:
        """#2896 review (blocking, impact 3): without the relocated root,
        `clear_expected_red_via_pr` reports "no expected_red entries found
        for this issue" for a relocated milestone that has them, so the
        clearing PR is never opened."""
        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true",
            entrypoint="tui/tests/acceptance.rs",
        )

        with patch(
            "coord.commands.acceptance.list_expected_red_via_api",
            return_value={"ms-65": {2282: {"ms65::a"}}},
        ), patch("coord.commands.acceptance.github_ops") as mock_gh, patch(
            "coord.acceptance.clear_expected_red_via_pr",
        ) as mock_clear:
            mock_gh.get_issue.return_value = {"state": "CLOSED"}
            mock_clear.return_value = "cleared expected_red for #2282: ms65::a (PR #501)"

            result = self._invoke(config_path)

        assert result.exit_code == 0, result.output
        assert mock_clear.call_args.kwargs["search_roots"] == [
            "tests/acceptance/", "tui/tests/acceptance/",
        ]

    def test_clear_with_issue_filter_on_an_open_issue_clears_nothing(
        self, tmp_path: Path,
    ) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true")

        with patch(
            "coord.commands.acceptance.list_expected_red_via_api",
            return_value={"ms01": {944: {"ms01::a"}, 945: {"ms01::z"}}},
        ), patch("coord.commands.acceptance.github_ops") as mock_gh, patch(
            "coord.acceptance.clear_expected_red_via_pr",
        ) as mock_clear:
            mock_gh.get_issue.side_effect = lambda repo, n: (
                {"state": "CLOSED"} if n == 944 else {"state": "OPEN"}
            )

            result = self._invoke(config_path, "--issue", "945")

        assert result.exit_code == 0, result.output
        mock_clear.assert_not_called()
        assert "not STUCK" in result.output

    def test_clear_with_issue_filter_on_the_stuck_issue_clears_only_it(
        self, tmp_path: Path,
    ) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true")

        with patch(
            "coord.commands.acceptance.list_expected_red_via_api",
            return_value={"ms01": {944: {"ms01::a"}, 945: {"ms01::z"}}},
        ), patch("coord.commands.acceptance.github_ops") as mock_gh, patch(
            "coord.acceptance.clear_expected_red_via_pr",
        ) as mock_clear:
            mock_gh.get_issue.side_effect = lambda repo, n: (
                {"state": "CLOSED"} if n == 944 else {"state": "OPEN"}
            )
            mock_clear.return_value = "cleared expected_red for #944: ms01::a (PR #501)"

            result = self._invoke(config_path, "--issue", "944")

        assert result.exit_code == 0, result.output
        mock_clear.assert_called_once()

    def test_no_stuck_entries_reports_nothing_to_clear(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true")

        with patch(
            "coord.commands.acceptance.list_expected_red_via_api",
            return_value={"ms01": {945: {"ms01::z"}}},
        ), patch("coord.commands.acceptance.github_ops") as mock_gh, patch(
            "coord.acceptance.clear_expected_red_via_pr",
        ) as mock_clear:
            mock_gh.get_issue.return_value = {"state": "OPEN"}

            result = self._invoke(config_path)

        assert result.exit_code == 0, result.output
        mock_clear.assert_not_called()
        assert "no STUCK entries to clear" in result.output

    def test_clear_success_writes_a_durable_audit_row(
        self, tmp_path: Path, coord_db,
    ) -> None:
        """#2266 scope 2: a cleared entry lands a queryable row — not just
        a CLI output line — so a re-run doesn't need to trust anyone read
        this command's stdout."""
        config_path = _write_config(tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true")

        with patch(
            "coord.commands.acceptance.list_expected_red_via_api",
            return_value={"ms01": {944: {"ms01::a"}}},
        ), patch("coord.commands.acceptance.github_ops") as mock_gh, patch(
            "coord.acceptance.clear_expected_red_via_pr",
        ) as mock_clear:
            mock_gh.get_issue.return_value = {"state": "CLOSED"}
            mock_clear.return_value = "cleared expected_red for #944: ms01::a (PR #501)"

            result = self._invoke(config_path)

        assert result.exit_code == 0, result.output
        rows = coord_db.execute(
            "SELECT event_type, issue FROM audit_log WHERE event_type = 'expected_red_clear'",
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["issue"] == 944

    def test_clear_failure_writes_a_distinct_durable_audit_row(
        self, tmp_path: Path, coord_db,
    ) -> None:
        """#2266 scope 2: a failed clear (the common case — `clear_expected_
        red_via_pr` never raises, it degrades to a `warning: ...` string)
        must be just as durable as a success, with a distinct event_type so
        it's queryable as "still stuck" rather than silently indistinguishable
        from a clear that worked. #2266 review non-blocking finding: a
        hard failure also now exits non-zero, so a CI/cron caller can
        detect a fully-failed run without reading stdout or the audit log."""
        config_path = _write_config(tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true")

        with patch(
            "coord.commands.acceptance.list_expected_red_via_api",
            return_value={"ms01": {944: {"ms01::a"}}},
        ), patch("coord.commands.acceptance.github_ops") as mock_gh, patch(
            "coord.acceptance.clear_expected_red_via_pr",
        ) as mock_clear:
            mock_gh.get_issue.return_value = {"state": "CLOSED"}
            mock_clear.return_value = "warning: could not open expected_red clear PR: boom"

            result = self._invoke(config_path)

        assert result.exit_code == 1, result.output
        rows = coord_db.execute(
            "SELECT event_type, issue FROM audit_log WHERE event_type = 'expected_red_clear_failed'",
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["issue"] == 944
        assert not coord_db.execute(
            "SELECT 1 FROM audit_log WHERE event_type = 'expected_red_clear'",
        ).fetchall()

    def test_clear_no_op_does_not_write_an_audit_row_or_fail(
        self, tmp_path: Path, coord_db,
    ) -> None:
        """#2266 review blocking finding 1: "nothing to clear" (e.g. a race
        — another process already cleared it) is not a failure — it must
        not write a durable `expected_red_clear_failed` row, nor fail the
        command's exit code."""
        config_path = _write_config(tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true")

        with patch(
            "coord.commands.acceptance.list_expected_red_via_api",
            return_value={"ms01": {944: {"ms01::a"}}},
        ), patch("coord.commands.acceptance.github_ops") as mock_gh, patch(
            "coord.acceptance.clear_expected_red_via_pr",
        ) as mock_clear:
            mock_gh.get_issue.return_value = {"state": "CLOSED"}
            mock_clear.return_value = "no expected_red entries for this issue"

            result = self._invoke(config_path)

        assert result.exit_code == 0, result.output
        assert not coord_db.execute(
            "SELECT 1 FROM audit_log WHERE event_type LIKE 'expected_red_clear%'",
        ).fetchall()

    def test_issue_without_clear_warns(self, tmp_path: Path) -> None:
        """#2266 review nit: `--issue` without `--clear` is silently
        accepted and ignored today despite its own help text saying it
        only takes effect with `--clear` — must warn instead."""
        config_path = _write_config(tmp_path, repo_path=str(tmp_path / "unused"), run_cmd="true")

        with patch(
            "coord.commands.acceptance.list_expected_red_via_api",
            return_value={"ms01": {944: {"ms01::a"}}},
        ):
            result = CliRunner().invoke(main, [
                "acceptance", "expected-red", "coord-tui", "--config", str(config_path),
                "--issue", "944",
            ])

        assert result.exit_code == 0, result.output
        assert "--issue has no effect without --clear" in result.output


class TestAcceptanceStall:
    """`coord acceptance stall` (#846) — the worker self-report path for a
    churning acceptance slice: pinned #603 context note, best-effort WIP
    push, one-shot 'needs attention' GitHub comment."""

    def test_stall_pushes_wip_records_context_and_posts_comment(
        self, tmp_path: Path, coord_db,
    ) -> None:
        from coord import state

        repo_dir = tmp_path / "repo"
        _init_git_repo(repo_dir)
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd="true")

        state.record_dispatched(
            assignment_id="aid-1",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        with patch("coord.commands.acceptance.github_ops") as mock_gh:
            result = CliRunner().invoke(main, [
                "acceptance", "stall", "--repo", "coord-tui", "--issue", "944",
                "--tried", "tightened the regex, retried the driver",
                "--stuck", "ms01::b keeps failing on the empty-input case",
                "--path", str(repo_dir), "--config", str(config_path),
            ])

        assert result.exit_code == 0, result.output
        assert "Recorded acceptance stall" in result.output
        assert "WIP snapshot pushed" in result.output

        assert mock_gh.post_issue_comment.called
        (repo_github, issue_number, body), _ = mock_gh.post_issue_comment.call_args
        assert repo_github == "acme/coord-tui"
        assert issue_number == 944
        assert "Not converging" in body
        assert "aid-1" in body
        assert "laptop" in body

        entries = state.list_issue_context("coord-tui", 944)
        assert any(
            "Acceptance stall reported" in e["body"]
            and e["pinned"]
            and e["source"] == "acceptance-stall"
            for e in entries
        )

        # #846 review: the self-report must share the notified-ledger with
        # the coordinator's wall-clock backstop, otherwise the same
        # assignment stays eligible for a second "needs attention" comment.
        notified = state.load_notified()
        assert "aid-1:needs-attention" in notified
        assert notified["aid-1:needs-attention"]["event"] == "needs_attention"

    def test_stall_no_double_notify_with_backstop(
        self, tmp_path: Path, coord_db,
    ) -> None:
        """After a self-reported stall, the coordinator's wall-clock backstop
        (`detect_needs_attention`) must not re-flag the same assignment."""
        from coord import config as config_mod
        from coord import notify, state
        from coord.models import Assignment, Board

        repo_dir = tmp_path / "repo"
        _init_git_repo(repo_dir)
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd="true")
        cfg = config_mod.load(config_path)

        state.record_dispatched(
            assignment_id="aid-2",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        with patch("coord.commands.acceptance.github_ops") as mock_gh:
            result = CliRunner().invoke(main, [
                "acceptance", "stall", "--repo", "coord-tui", "--issue", "944",
                "--tried", "x", "--stuck", "y",
                "--path", str(repo_dir), "--config", str(config_path),
            ])
        assert result.exit_code == 0, result.output
        assert mock_gh.post_issue_comment.called

        # Simulate the assignment continuing to thrash after the self-report
        # (review_iteration climbing to convergence_rounds) — the exact
        # scenario from the review finding. Without the ledger write this
        # would still flag `aid-2` a second time.
        state.save_board(Board(
            active=[
                Assignment(
                    assignment_id="aid-2",
                    machine_name="laptop",
                    repo_name="coord-tui",
                    issue_number=944,
                    issue_title="oracle loop runner",
                    status="running",
                    type="work",
                    review_iteration=cfg.pipeline.convergence_rounds,
                )
            ],
            completed=[],
        ))

        detections = notify.detect_needs_attention(cfg)
        assert not any(
            detection.assignment_id == "aid-2" for detection, _ in detections
        )

    def test_stall_without_work_assignment_still_reports(
        self, tmp_path: Path, coord_db,
    ) -> None:
        """No dispatched work row for this issue yet — still push, note, and
        post a comment (assignment id just comes back blank)."""
        from coord import state

        repo_dir = tmp_path / "repo"
        _init_git_repo(repo_dir)
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd="true")

        with patch("coord.commands.acceptance.github_ops") as mock_gh:
            result = CliRunner().invoke(main, [
                "acceptance", "stall", "--repo", "coord-tui", "--issue", "944",
                "--tried", "x", "--stuck", "y",
                "--path", str(repo_dir), "--config", str(config_path),
            ])

        assert result.exit_code == 0, result.output
        assert mock_gh.post_issue_comment.called
        entries = state.list_issue_context("coord-tui", 944)
        assert any("Acceptance stall reported" in e["body"] for e in entries)

        # No work assignment id was resolved, so there's nothing to mark in
        # the notified-ledger (mirrors the blank-assignment-id comment body).
        notified = state.load_notified()
        assert not any(key.endswith(":needs-attention") for key in notified)

    def test_stall_unknown_repo_errors(self, tmp_path: Path, coord_db) -> None:
        repo_dir = tmp_path / "repo"
        _init_git_repo(repo_dir)
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd="true")

        result = CliRunner().invoke(main, [
            "acceptance", "stall", "--repo", "nope", "--issue", "944",
            "--tried", "x", "--stuck", "y",
            "--path", str(repo_dir), "--config", str(config_path),
        ])
        assert result.exit_code != 0
        assert "unknown repo" in result.output


class TestAcceptanceCapabilityRouting:
    """#966: `coord acceptance run --all` / `record` fail loudly instead of
    silently executing when this host lacks a capability the driver
    declares and another configured machine has it — no remote-exec
    plumbing to actually route there yet, so a clear error is the best
    available behavior."""

    CONFIG_YAML = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: here
    host: here.tail
    repos: [coord-tui]
    repo_paths:
      coord-tui: {repo_path}
  - name: capable
    host: capable.tail
    repos: [coord-tui]
    capabilities: [browser]
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: {run_cmd}
      capability: browser
"""

    def _config(self, tmp_path: Path, *, repo_path: str, run_cmd: str) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(self.CONFIG_YAML.format(repo_path=repo_path, run_cmd=json.dumps(run_cmd)))
        return p

    def test_run_all_fails_when_local_host_lacks_capability(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        cwd = tmp_path / "repo"
        cwd.mkdir()
        config_path = self._config(tmp_path, repo_path=str(cwd), run_cmd="echo '{}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "lacks the 'browser' capability" in result.output
        assert "'capable'" in result.output
        assert "#966" in result.output

    def test_run_all_proceeds_when_local_host_has_capability(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "capable")
        cwd = tmp_path / "repo"
        cwd.mkdir()
        # "capable" has no repo_paths entry, but --path is passed explicitly
        # so find_local_repo_path is never consulted for this command.
        config_path = self._config(tmp_path, repo_path=str(cwd), run_cmd="echo '{\"tests\": []}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all",
            "--path", str(cwd), "--config", str(config_path),
        ])
        # No capability gap → falls through to the ordinary "0 tests" exit.
        assert "lacks the" not in result.output
        assert result.exit_code == 1  # total == 0 → non-green, unrelated to #966

    def test_record_fails_when_local_host_lacks_capability(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: "here")
        config_path = self._config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo '{}'",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", "deadbeef", "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "lacks the 'browser' capability" in result.output
        assert "'capable'" in result.output


class TestAcceptanceAuthor:
    """`coord acceptance author` (#931) — thin CLI glue over
    `coord.test_author.dispatch_test_author`. The dispatch logic itself
    (machine picking, briefing content, error surfaces) is unit-tested in
    tests/test_test_author.py; these just check the CLI wiring: arguments
    reach the function correctly and its outcomes map to the right exit
    code/output."""

    def _config_no_driver(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: coord-tui\n"
            "    github: acme/coord-tui\n"
            "machines:\n"
            "  - name: laptop\n"
            "    host: laptop.tail\n"
            "    repos: [coord-tui]\n"
            "    repo_paths:\n"
            "      coord-tui: /tmp/repo\n"
        )
        return p

    def test_happy_path_reports_dispatch(self, tmp_path: Path, monkeypatch) -> None:
        config_path = self._config_no_driver(tmp_path)
        calls = {}

        def fake_dispatch(
            repo, tracking_issue, cfg, *,
            issue_number=None, machine_override=None, path=None,
        ):
            calls.update(
                repo=repo, tracking_issue=tracking_issue,
                issue_number=issue_number, machine_override=machine_override,
            )
            return ("aid-42", "laptop")

        monkeypatch.setattr("coord.test_author.dispatch_test_author", fake_dispatch)

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947", "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "aid-42" in result.output
        assert "laptop" in result.output
        assert "full milestone" in result.output
        assert calls == {
            "repo": "coord-tui", "tracking_issue": 947,
            "issue_number": None, "machine_override": None,
        }

    def test_issue_scope_and_machine_override_forwarded(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        config_path = self._config_no_driver(tmp_path)
        calls = {}

        def fake_dispatch(
            repo, tracking_issue, cfg, *,
            issue_number=None, machine_override=None, path=None,
        ):
            calls.update(issue_number=issue_number, machine_override=machine_override)
            return ("aid-7", "dellserver")

        monkeypatch.setattr("coord.test_author.dispatch_test_author", fake_dispatch)

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947",
            "--issue", "101", "--machine", "dellserver",
            "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "issue #101 slice" in result.output
        assert calls == {"issue_number": 101, "machine_override": "dellserver"}

    def test_for_path_forwarded_to_dispatch(self, tmp_path: Path, monkeypatch) -> None:
        """#1125: --for-path (routed-driver resolution) reaches
        dispatch_test_author as `path=`."""
        config_path = self._config_no_driver(tmp_path)
        calls = {}

        def fake_dispatch(
            repo, tracking_issue, cfg, *,
            issue_number=None, machine_override=None, path=None,
        ):
            calls["path"] = path
            return ("aid-99", "laptop")

        monkeypatch.setattr("coord.test_author.dispatch_test_author", fake_dispatch)

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947",
            "--for-path", "coord/acceptance.py", "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert calls["path"] == "coord/acceptance.py"

    def test_dispatch_error_surfaces_nonzero_exit(self, tmp_path: Path, monkeypatch) -> None:
        config_path = self._config_no_driver(tmp_path)

        def fake_dispatch(*a, **kw):
            raise RuntimeError("no acceptance driver configured for repo 'coord-tui'")

        monkeypatch.setattr("coord.test_author.dispatch_test_author", fake_dispatch)

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947", "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "no acceptance driver configured" in result.output

    def test_gate_a_refusal_exits_dispatch_refused_not_terminal_failure(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """#2063: the Gate-A "no recorded sign-off" refusal must map to
        `EXIT_DISPATCH_REFUSED` (5), not the generic terminal-failure exit
        (1) every other `dispatch_test_author` failure uses — otherwise
        `coord drive`'s subprocess boundary can't tell this deterministic,
        operator-fixable refusal apart from a crash, and `coord drive-queue`
        parks it (#1891/#1892) instead of burning attempts toward terminal
        `blocked` (#2040) on a contract nobody approved."""
        from coord.dispatch import DispatchRefused
        from coord.drive import EXIT_DISPATCH_REFUSED

        config_path = self._config_no_driver(tmp_path)

        def fake_dispatch(*a, **kw):
            raise DispatchRefused(
                "Gate A has no recorded human sign-off for ms-37 "
                "[gate-a-approval repo=coord-tui ms-37 v=none]"
            )

        monkeypatch.setattr("coord.test_author.dispatch_test_author", fake_dispatch)

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947", "--config", str(config_path),
        ])
        assert result.exit_code == EXIT_DISPATCH_REFUSED
        assert "no recorded human sign-off" in result.output


class TestAcceptanceAuthorInteractive:
    """#1173: `coord acceptance author --interactive` — thin CLI glue over
    `coord.test_author.dispatch_test_author_interactive`. Mirrors
    TestAcceptanceAuthor's split: the dispatch logic itself (branch
    continuation, board row shape, local/remote launch) is unit-tested in
    tests/test_test_author.py; these just check the CLI wiring — the
    `--interactive`/`--dry-run` flags reach the right function with the
    right arguments, and it is the interactive function that runs, never
    the headless one."""

    def _config_no_driver(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: coord-tui\n"
            "    github: acme/coord-tui\n"
            "machines:\n"
            "  - name: laptop\n"
            "    host: laptop.tail\n"
            "    repos: [coord-tui]\n"
            "    repo_paths:\n"
            "      coord-tui: /tmp/repo\n"
        )
        return p

    def test_interactive_dispatches_interactive_function_not_headless(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        config_path = self._config_no_driver(tmp_path)
        calls = {}

        def fake_interactive(
            repo, tracking_issue, cfg, *,
            issue_number=None, machine_override=None, path=None, dry_run=False,
        ):
            calls.update(
                repo=repo, tracking_issue=tracking_issue,
                issue_number=issue_number, machine_override=machine_override,
                path=path, dry_run=dry_run,
            )
            return 0

        def fake_headless(*a, **kw):
            raise AssertionError("--interactive must not fall through to headless dispatch")

        monkeypatch.setattr(
            "coord.test_author.dispatch_test_author_interactive", fake_interactive,
        )
        monkeypatch.setattr("coord.test_author.dispatch_test_author", fake_headless)

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947", "--interactive",
            "--issue", "101", "--machine", "dellserver",
            "--for-path", "coord/acceptance.py",
            "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert calls == {
            "repo": "coord-tui", "tracking_issue": 947,
            "issue_number": 101, "machine_override": "dellserver",
            "path": "coord/acceptance.py", "dry_run": False,
        }

    def test_interactive_dry_run_forwarded(self, tmp_path: Path, monkeypatch) -> None:
        config_path = self._config_no_driver(tmp_path)
        calls = {}

        def fake_interactive(repo, tracking_issue, cfg, **kw):
            calls.update(kw)
            return 0

        monkeypatch.setattr(
            "coord.test_author.dispatch_test_author_interactive", fake_interactive,
        )

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947",
            "--interactive", "--dry-run", "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert calls["dry_run"] is True

    def test_interactive_nonzero_exit_code_propagates(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        config_path = self._config_no_driver(tmp_path)
        monkeypatch.setattr(
            "coord.test_author.dispatch_test_author_interactive",
            lambda *a, **kw: 1,
        )

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947", "--interactive",
            "--config", str(config_path),
        ])
        assert result.exit_code == 1

    def test_interactive_error_surfaces_nonzero_exit(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        config_path = self._config_no_driver(tmp_path)

        def fake_interactive(*a, **kw):
            raise RuntimeError("no machine claims repo 'coord-tui'")

        monkeypatch.setattr(
            "coord.test_author.dispatch_test_author_interactive", fake_interactive,
        )

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947", "--interactive",
            "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "no machine claims repo" in result.output

    def test_gate_a_refusal_exits_dispatch_refused_not_terminal_failure(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """#2063: same as the headless `TestAcceptanceAuthor` case above —
        the `--interactive` branch has its own `except RuntimeError` and
        must classify the Gate-A refusal identically, or `coord drive`'s
        `--interactive`-attended JIT-authoring path falls through to
        terminal `blocked` while the headless path parks correctly."""
        from coord.dispatch import DispatchRefused
        from coord.drive import EXIT_DISPATCH_REFUSED

        config_path = self._config_no_driver(tmp_path)

        def fake_interactive(*a, **kw):
            raise DispatchRefused(
                "Gate A has no recorded human sign-off for ms-37 "
                "[gate-a-approval repo=coord-tui ms-37 v=none]"
            )

        monkeypatch.setattr(
            "coord.test_author.dispatch_test_author_interactive", fake_interactive,
        )

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947", "--interactive",
            "--config", str(config_path),
        ])
        assert result.exit_code == EXIT_DISPATCH_REFUSED
        assert "no recorded human sign-off" in result.output

    def test_dry_run_without_interactive_errors(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        config_path = self._config_no_driver(tmp_path)

        def fake_headless(*a, **kw):
            raise AssertionError("--dry-run without --interactive must not dispatch anything")

        monkeypatch.setattr("coord.test_author.dispatch_test_author", fake_headless)

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947", "--dry-run",
            "--config", str(config_path),
        ])
        assert result.exit_code == 2
        assert "--dry-run requires --interactive" in result.output

    def test_non_interactive_still_uses_headless_dispatch(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Without --interactive the pre-#1173 headless behavior is
        unchanged — no regression from adding the new flag."""
        config_path = self._config_no_driver(tmp_path)

        def fake_headless(repo, tracking_issue, cfg, **kw):
            return ("aid-1", "laptop")

        def fake_interactive(*a, **kw):
            raise AssertionError("must not run the interactive path by default")

        monkeypatch.setattr("coord.test_author.dispatch_test_author", fake_headless)
        monkeypatch.setattr(
            "coord.test_author.dispatch_test_author_interactive", fake_interactive,
        )

        result = CliRunner().invoke(main, [
            "acceptance", "author", "coord-tui", "947", "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "aid-1" in result.output


class TestAcceptanceMockThinClientRefusal:
    """#2018 / #2748 IL-2: `coord acceptance mock` used to silently no-op
    from a thin client (exit 0, no output, no dispatch) — the operator's
    laptop, which is where the customer/oracle loop is actually driven from
    (docs/ORACLE_LOOP.md). It must now refuse loudly, naming the fix,
    mirroring `coord.commands.portal._refuse_if_thin_client`."""

    def test_refuses_on_a_thin_client(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("COORD_SERVICE_URL", "http://dellserver:7435")
        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        result = CliRunner().invoke(main, [
            "acceptance", "mock", "coord-tui", "100", "--config", str(config_path),
        ])
        assert result.exit_code != 0
        assert "must run on the daemon host" in result.output
        assert "dellserver" in result.output
        assert "#2018" in result.output

    def test_never_calls_gh_or_dispatches_on_a_thin_client(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """The refusal must fire BEFORE any `gh` call or dispatch attempt —
        not just before the daemon write. A guard that ran after the `gh`
        fetch would still leak an unauthenticated/wrong-identity call."""
        from unittest.mock import patch

        monkeypatch.setenv("COORD_SERVICE_URL", "http://dellserver:7435")
        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        with patch("coord.github_ops.get_issue") as get_issue, \
             patch("coord.dispatch.dispatch_with_retry") as dispatch:
            result = CliRunner().invoke(main, [
                "acceptance", "mock", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code != 0
        get_issue.assert_not_called()
        dispatch.assert_not_called()

    def test_amend_mode_also_refuses(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("COORD_SERVICE_URL", "http://dellserver:7435")
        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        result = CliRunner().invoke(main, [
            "acceptance", "mock", "coord-tui", "100", "--amend", "fix the CTA copy",
            "--config", str(config_path),
        ])
        assert result.exit_code != 0
        assert "must run on the daemon host" in result.output

    def test_not_a_thin_client_is_unaffected(self, tmp_path: Path, monkeypatch) -> None:
        """No `board_service` configured (the daemon host itself, or a
        single-machine setup) must dispatch exactly as before — this is a
        refusal for thin clients only, never a blanket regression."""
        from unittest.mock import patch

        from coord.models import Board

        monkeypatch.delenv("COORD_SERVICE_URL", raising=False)
        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        issue_data = {
            "number": 100, "title": "Milestone tracker", "body": "",
            "milestone": {"number": 9, "title": "Q3"},
        }
        with patch("coord.github_ops.get_issue", return_value=issue_data), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch(
                 "coord.dispatch.dispatch_with_retry",
                 return_value={"id": "mock-asg-1"},
             ), \
             patch("coord.dispatch.post_briefing"), \
             patch("coord.state.record_dispatched"):
            result = CliRunner().invoke(main, [
                "acceptance", "mock", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 0, result.output


class TestAcceptanceMock:
    """#930 (Gate A): `coord acceptance mock <repo> <tracking_issue>`
    dispatches the mock-author. Mocks `coord.github_ops`, `coord.
    board_service.read_board`, and `coord.dispatch.dispatch_with_retry` so
    the test drives the real Click command end to end without a live `gh`
    call or HTTP POST to an agent."""

    def test_dispatches_mock_author_and_prints_assignment_id(
        self, tmp_path: Path,
    ) -> None:
        from unittest.mock import patch

        from coord.models import Board

        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        issue_data = {
            "number": 100, "title": "Milestone tracker", "body": "",
            "milestone": {"number": 9, "title": "Q3"},
        }
        with patch("coord.github_ops.get_issue", return_value=issue_data), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch(
                 "coord.dispatch.dispatch_with_retry",
                 return_value={"id": "mock-asg-1"},
             ) as disp, \
             patch("coord.dispatch.post_briefing"), \
             patch("coord.state.record_dispatched") as mock_record:
            result = CliRunner().invoke(main, [
                "acceptance", "mock", "coord-tui", "100", "--config", str(config_path),
            ])

        assert result.exit_code == 0, result.output
        assert "laptop" in result.output
        assert "mock-asg-1" in result.output
        disp.assert_called_once()
        proposal = disp.call_args[0][0]
        assert proposal.type == "mock-author"
        assert proposal.target_branch == "ms-9-gate-a"
        mock_record.assert_called_once()

    def test_for_path_forwarded_to_dispatch(self, tmp_path: Path, monkeypatch) -> None:
        """#1125: --for-path (routed-driver resolution) reaches
        dispatch_acceptance_mock as `path=`."""
        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        calls = {}

        def fake_dispatch(
            repo, tracking_issue, cfg, *,
            machine_override=None, path=None, amend_briefing=None,
        ):
            calls["path"] = path
            calls["amend_briefing"] = amend_briefing
            return ("mock-asg-2", "laptop")

        monkeypatch.setattr("coord.mock_author.dispatch_acceptance_mock", fake_dispatch)

        result = CliRunner().invoke(main, [
            "acceptance", "mock", "coord-tui", "100",
            "--for-path", "coord/acceptance.py", "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert calls["path"] == "coord/acceptance.py"

    def test_amend_forwarded_to_dispatch(self, tmp_path: Path, monkeypatch) -> None:
        """#1315: --amend reaches dispatch_acceptance_mock as amend_briefing=."""
        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        calls = {}

        def fake_dispatch(
            repo, tracking_issue, cfg, *,
            machine_override=None, path=None, amend_briefing=None,
        ):
            calls["amend_briefing"] = amend_briefing
            return ("mock-asg-amend", "laptop")

        monkeypatch.setattr("coord.mock_author.dispatch_acceptance_mock", fake_dispatch)

        result = CliRunner().invoke(main, [
            "acceptance", "mock", "coord-tui", "100",
            "--amend", "fix the CLI flag name in the contract",
            "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert calls["amend_briefing"] == "fix the CLI flag name in the contract"
        assert "mock-asg-amend" in result.output
        assert "amend" in result.output

    def test_amend_file_forwarded_to_dispatch(self, tmp_path: Path, monkeypatch) -> None:
        """--amend-file reads the correction text from disk."""
        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        amend_file = tmp_path / "amend.txt"
        amend_file.write_text("the mock glob should be *.screen, not *.txt\n")
        calls = {}

        def fake_dispatch(
            repo, tracking_issue, cfg, *,
            machine_override=None, path=None, amend_briefing=None,
        ):
            calls["amend_briefing"] = amend_briefing
            return ("mock-asg-amend-2", "laptop")

        monkeypatch.setattr("coord.mock_author.dispatch_acceptance_mock", fake_dispatch)

        result = CliRunner().invoke(main, [
            "acceptance", "mock", "coord-tui", "100",
            "--amend-file", str(amend_file),
            "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert calls["amend_briefing"] == "the mock glob should be *.screen, not *.txt\n"

    def test_amend_and_amend_file_are_mutually_exclusive(self, tmp_path: Path) -> None:
        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        amend_file = tmp_path / "amend.txt"
        amend_file.write_text("text")
        result = CliRunner().invoke(main, [
            "acceptance", "mock", "coord-tui", "100",
            "--amend", "inline text", "--amend-file", str(amend_file),
            "--config", str(config_path),
        ])
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_unknown_repo_errors(self, tmp_path: Path) -> None:
        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        result = CliRunner().invoke(main, [
            "acceptance", "mock", "nope", "100", "--config", str(config_path),
        ])
        assert result.exit_code == 2
        assert "unknown repo" in result.output

    def test_no_milestone_errors(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        config_path = _write_config(
            tmp_path, repo_path=str(tmp_path / "repo"), run_cmd="echo {}",
        )
        with patch(
            "coord.github_ops.get_issue",
            return_value={"number": 100, "title": "t", "body": "", "milestone": None},
        ):
            result = CliRunner().invoke(main, [
                "acceptance", "mock", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 1
        assert "no milestone" in result.output
