"""#2220: end-to-end behaviour of ``coord repo add`` and ``coord repo doctor``,
plus the ``coord doctor`` wiring.

``coord repo doctor`` exists to **gate**, so its exit code is as much a part of
the contract as its output: 1 on any CRIT, 0 when the only residue is warnings.
And ``coord repo add`` writes the file that governs the whole fleet, so the
tests that matter most here are the ones proving it *refuses* — no
``coord-settings`` checkout, an unreadable default branch, an edit that would
not parse.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import coord.network as network_mod
from coord import github_ops
from coord.commands.repo import repo_add, repo_doctor
from coord.network import ONLINE, MachineStatus


CONFIG = """\
repos:
  - name: api
    github: acme/api
    depends_on: []
    default_branch: main
    test_command: "make test"

machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [api]
    repo_paths:
      api: ~/src/api
  - name: dellserver
    host: dellserver.tailnet
    capabilities: [python]
    repos: [api]
    repo_paths:
      api: ~/src/api
"""

# #3073: a repo with no test_command/ci_command at all — the Test stage
# cannot resolve a command and the gate silently no-ops without it.
NO_TEST_COMMAND_CONFIG = """\
repos:
  - name: api
    github: acme/api
    depends_on: []
    default_branch: main

machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [api]
    repo_paths:
      api: ~/src/api
  - name: dellserver
    host: dellserver.tailnet
    capabilities: [python]
    repos: [api]
    repo_paths:
      api: ~/src/api
"""

# #3073: a `kind` declared ahead of its adapter — legal (SUPPORTED_KINDS is
# the source of truth on what `run_driver()` will actually execute), so this
# must WARN, never CRIT.
UNSUPPORTED_KIND_CONFIG = CONFIG + """\
acceptance:
  drivers:
    api:
      kind: future-framework
      run: "run the suite"
"""

# #3073: uat_preview configured — used to prove presence alone still WARNs
# (#2948: a configured preview is not the same as a working one).
UAT_PREVIEW_CONFIG = """\
repos:
  - name: api
    github: acme/api
    depends_on: []
    default_branch: main
    test_command: "make test"
    uat_preview: "https://example.com/preview/{branch}"

machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [api]
    repo_paths:
      api: ~/src/api
  - name: dellserver
    host: dellserver.tailnet
    capabilities: [python]
    repos: [api]
    repo_paths:
      api: ~/src/api
"""


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG)
    return p


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Keep `local_clone_path`'s ~/src/<repo> fallback off the developer's own
    machine — otherwise the graph layer's verdict depends on who runs pytest."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))


def _status(machine, repos, degraded=None):
    return MachineStatus(
        machine=machine, state=ONLINE, latency_ms=2.0,
        health={
            "machine": machine.name,
            "capabilities": list(machine.capabilities),
            "repos": list(repos),
            "degraded": dict(degraded or {}),
        },
    )


def _stub_github(monkeypatch, *, labels, workflows, files, default_branch="main"):
    monkeypatch.setattr(
        github_ops, "get_repo_default_branch", lambda repo: default_branch
    )
    monkeypatch.setattr(github_ops, "list_repo_labels", lambda repo: list(labels))
    monkeypatch.setattr(github_ops, "list_repo_workflows", lambda repo: list(workflows))

    def _file(repo, path, branch):
        if path not in files:
            raise RuntimeError("not found")
        return files[path]

    monkeypatch.setattr(github_ops, "get_repo_file", _file)
    monkeypatch.setattr(
        github_ops, "repo_file_exists", lambda repo, path, branch: path in files
    )


PR_WORKFLOW = "on:\n  pull_request:\njobs: {}\n"


class TestRepoDoctorCommand:
    def test_exits_one_and_names_the_agent_skew(self, config_path, monkeypatch):
        """#2220's first acceptance bullet, in miniature: the dellserver agent
        skew is reported from LIVE state while config says everything is fine."""
        from coord.config import load

        cfg = load(config_path)
        laptop, dellserver = cfg.machines
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [_status(laptop, ["api"]), _status(dellserver, [])],
        )
        _stub_github(
            monkeypatch,
            labels=["coord", "tier:small", "tier:large"],
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={".github/workflows/ci.yml": PR_WORKFLOW, "CLAUDE.md": "#"},
        )

        result = CliRunner().invoke(
            repo_doctor, ["api", "--config", str(config_path)], catch_exceptions=False
        )
        assert result.exit_code == 1, result.output
        assert "machines.agent_repo_skew" in result.output
        assert "dellserver" in result.output
        # #2299: the remedy is no longer "restart coord-agent" — agents
        # re-read coordinator.yml themselves, so the first move is to wait a
        # poll, not to kill every live worker on that machine.
        assert "wait one /health poll" in result.output
        # The healthy machine must not be dragged in with it.
        assert result.output.count("machines.agent_repo_skew") == 1
        assert "REPO_DOCTOR: repo=api" in result.output

    def test_exits_zero_on_a_fully_onboarded_repo(self, config_path, monkeypatch):
        from coord.config import load

        cfg = load(config_path)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [_status(m, ["api"]) for m in cfg.machines],
        )
        _stub_github(
            monkeypatch,
            labels=["coord", "tier:small", "tier:large"],
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={".github/workflows/ci.yml": PR_WORKFLOW, "CLAUDE.md": "#"},
        )
        result = CliRunner().invoke(
            repo_doctor, ["api", "--config", str(config_path)], catch_exceptions=False
        )
        assert result.exit_code == 0, result.output
        assert "ok=true" in result.output

    def test_no_github_flag_makes_it_offline(self, config_path, monkeypatch):
        """Every `gh` helper is left un-stubbed: if --no-github leaked a single
        call, this raises rather than quietly shelling out in CI."""
        from coord.config import load

        cfg = load(config_path)

        def _boom(*a, **k):
            raise AssertionError("--no-github must make zero gh calls")

        for name in (
            "get_repo_default_branch", "list_repo_labels",
            "list_repo_workflows", "repo_file_exists",
        ):
            monkeypatch.setattr(github_ops, name, _boom)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [_status(m, ["api"]) for m in cfg.machines],
        )
        result = CliRunner().invoke(
            repo_doctor,
            ["api", "--config", str(config_path), "--no-github"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

    def test_unknown_repo_reports_the_named_finding_and_exits_one(
        self, config_path, monkeypatch
    ):
        monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: [])
        result = CliRunner().invoke(
            repo_doctor, ["ghost", "--config", str(config_path)], catch_exceptions=False
        )
        assert result.exit_code == 1
        assert "config.repo_missing" in result.output

    def test_verbose_shows_passing_checks(self, config_path, monkeypatch):
        from coord.config import load

        cfg = load(config_path)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [_status(m, ["api"]) for m in cfg.machines],
        )
        _stub_github(
            monkeypatch,
            labels=["coord", "tier:small", "tier:large"],
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={".github/workflows/ci.yml": PR_WORKFLOW, "CLAUDE.md": "#"},
        )
        result = CliRunner().invoke(
            repo_doctor,
            ["api", "--config", str(config_path), "--verbose"],
            catch_exceptions=False,
        )
        assert "machines.servable" in result.output
        assert "github.coord_label_present" in result.output


class TestReadinessLayer:
    """#3073: a repo can clear all five onboarding layers and still not be
    fit for the pipeline it is actually enrolled in — no test_command, no
    uat_preview on a portal-linked repo, or a milestone opted into the
    oracle loop against a driver that was never configured. Severity is
    driven by enrolment, not a fixed list."""

    def test_no_test_command_fails_and_names_the_repo(self, monkeypatch, tmp_path):
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(NO_TEST_COMMAND_CONFIG)
        from coord.config import load

        cfg = load(config_path)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [_status(m, ["api"]) for m in cfg.machines],
        )
        _stub_github(
            monkeypatch,
            labels=["coord", "tier:small", "tier:large"],
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={".github/workflows/ci.yml": PR_WORKFLOW, "CLAUDE.md": "#"},
        )
        result = CliRunner().invoke(
            repo_doctor, ["api", "--config", str(config_path)], catch_exceptions=False
        )
        assert result.exit_code == 1, result.output
        assert "contents.test_command_unresolved" in result.output
        assert "api" in result.output
        assert "Test stage" in result.output

    def test_kind_outside_supported_kinds_warns_not_fails(self, monkeypatch, tmp_path):
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(UNSUPPORTED_KIND_CONFIG)
        from coord.config import load

        cfg = load(config_path)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [_status(m, ["api"]) for m in cfg.machines],
        )
        _stub_github(
            monkeypatch,
            labels=["coord", "tier:small", "tier:large"],
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={".github/workflows/ci.yml": PR_WORKFLOW, "CLAUDE.md": "#"},
        )
        result = CliRunner().invoke(
            repo_doctor, ["api", "--config", str(config_path)], catch_exceptions=False
        )
        assert result.exit_code == 0, result.output
        assert "oracle.kind_unsupported" in result.output
        assert "future-framework" in result.output

    def test_tests_acceptance_authored_with_no_driver_fails(
        self, config_path, monkeypatch
    ):
        """Someone already ran `coord acceptance mock` (tests/acceptance/
        exists on the default branch) but coordinator.yml never got an
        `acceptance.drivers` entry — not the legitimate 'hasn't joined yet'
        WARN, a milestone here can be opted into a driver that doesn't
        exist."""
        from coord.config import load

        cfg = load(config_path)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [_status(m, ["api"]) for m in cfg.machines],
        )
        _stub_github(
            monkeypatch,
            labels=["coord", "tier:small", "tier:large"],
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={
                ".github/workflows/ci.yml": PR_WORKFLOW, "CLAUDE.md": "#",
                "tests/acceptance": "ms-01/contract.md",
            },
        )
        result = CliRunner().invoke(
            repo_doctor, ["api", "--config", str(config_path)], catch_exceptions=False
        )
        assert result.exit_code == 1, result.output
        assert "oracle.opted_in_no_driver" in result.output

    def test_portal_linked_no_uat_preview_warns_quality_check_unreachable(
        self, config_path, monkeypatch
    ):
        from coord import portal_store
        from coord.config import load

        portal_store.link_issue(repo_name="api", issue_number=1, submission_id="SUB-1")
        cfg = load(config_path)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [_status(m, ["api"]) for m in cfg.machines],
        )
        _stub_github(
            monkeypatch,
            labels=["coord", "tier:small", "tier:large"],
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={".github/workflows/ci.yml": PR_WORKFLOW, "CLAUDE.md": "#"},
        )
        result = CliRunner().invoke(
            repo_doctor, ["api", "--config", str(config_path)], catch_exceptions=False
        )
        assert result.exit_code == 0, result.output  # WARN, not a gate
        assert "contents.uat_preview_missing" in result.output
        assert "quality-check" in result.output
        assert "unreachable" in result.output

    def test_unlinked_repo_with_no_uat_preview_is_silent(self, config_path, monkeypatch):
        """A repo that has never been portal-linked has no timeline to
        stall — the UAT-readiness check must not fire at all."""
        from coord.config import load

        cfg = load(config_path)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [_status(m, ["api"]) for m in cfg.machines],
        )
        _stub_github(
            monkeypatch,
            labels=["coord", "tier:small", "tier:large"],
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={".github/workflows/ci.yml": PR_WORKFLOW, "CLAUDE.md": "#"},
        )
        result = CliRunner().invoke(
            repo_doctor, ["api", "--config", str(config_path)], catch_exceptions=False
        )
        assert result.exit_code == 0, result.output
        assert "contents.uat_preview_missing" not in result.output
        assert "contents.uat_preview_unverified" not in result.output

    def test_portal_linked_uat_preview_configured_still_warns_unverified(
        self, monkeypatch, tmp_path
    ):
        """#2948: a CONFIGURED uat_preview is not the same as a WORKING one
        — presence alone must not read as green."""
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(UAT_PREVIEW_CONFIG)
        from coord import portal_store
        from coord.config import load

        portal_store.link_issue(repo_name="api", issue_number=1, submission_id="SUB-1")
        cfg = load(config_path)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [_status(m, ["api"]) for m in cfg.machines],
        )
        _stub_github(
            monkeypatch,
            labels=["coord", "tier:small", "tier:large"],
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={".github/workflows/ci.yml": PR_WORKFLOW, "CLAUDE.md": "#"},
        )
        result = CliRunner().invoke(
            repo_doctor, ["api", "--config", str(config_path)], catch_exceptions=False
        )
        assert result.exit_code == 0, result.output  # WARN, not a gate
        assert "contents.uat_preview_missing" not in result.output
        assert "contents.uat_preview_unverified" in result.output
        assert "2948" in result.output

    def test_fully_configured_repo_reports_clean(self, config_path, monkeypatch):
        """No test_command gap, a supported driver kind, no unauthored
        oracle evidence, and no portal link at all — the readiness layer
        must add nothing to a repo that is genuinely fit for its pipeline."""
        from coord.config import load

        cfg = load(config_path)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [_status(m, ["api"]) for m in cfg.machines],
        )
        _stub_github(
            monkeypatch,
            labels=["coord", "tier:small", "tier:large"],
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={".github/workflows/ci.yml": PR_WORKFLOW, "CLAUDE.md": "#"},
        )
        result = CliRunner().invoke(
            repo_doctor, ["api", "--config", str(config_path)], catch_exceptions=False
        )
        assert result.exit_code == 0, result.output
        assert "ok=true" in result.output
        assert "contents.test_command_unresolved" not in result.output
        assert "oracle.opted_in_no_driver" not in result.output
        assert "contents.uat_preview_missing" not in result.output
        assert "oracle.kind_unsupported" not in result.output

    def test_fix_does_not_invent_test_command_or_preview_url(
        self, monkeypatch, tmp_path
    ):
        """``--fix`` only repairs graphify's machine-local half — it must
        never write a `test_command`/`uat_preview` into coordinator.yml on
        the strength of a CRIT/WARN this layer reports (#3073)."""
        import coord.commands.repo as repo_cmd

        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(NO_TEST_COMMAND_CONFIG)
        from coord.config import load

        cfg = load(config_path)
        monkeypatch.setattr(
            network_mod, "check_all",
            lambda *a, **k: [_status(m, ["api"]) for m in cfg.machines],
        )
        monkeypatch.setattr(repo_cmd, "_run_graph_fix", lambda *a, **k: None)
        _stub_github(
            monkeypatch,
            labels=["coord", "tier:small", "tier:large"],
            workflows=[{"name": "CI", "path": ".github/workflows/ci.yml"}],
            files={".github/workflows/ci.yml": PR_WORKFLOW, "CLAUDE.md": "#"},
        )
        before = config_path.read_text()
        result = CliRunner().invoke(
            repo_doctor,
            ["api", "--config", str(config_path), "--fix"],
            catch_exceptions=False,
        )
        # Still fails (nothing was repaired) — but nothing was WRITTEN either.
        assert result.exit_code == 1, result.output
        assert config_path.read_text() == before
        assert "test_command" not in config_path.read_text()
        assert "uat_preview" not in config_path.read_text()


class TestRepoAddCommand:
    def test_writes_the_entry_and_prints_the_residue(self, config_path, monkeypatch):
        created: list[tuple[str, str]] = []
        monkeypatch.setattr(
            github_ops, "get_repo_default_branch", lambda repo: "develop"
        )
        monkeypatch.setattr(
            github_ops, "create_label",
            lambda repo, label, **k: created.append((repo, label)),
        )

        result = CliRunner().invoke(
            repo_add,
            [
                "newrepo", "--github", "acme/newrepo",
                "--machines", "laptop,dellserver",
                "--config", str(config_path),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        from coord.config import load

        cfg = load(config_path)
        repo = cfg.repo("newrepo")
        assert repo is not None
        # Read from GitHub, NOT defaulted to main.
        assert repo.default_branch == "develop"
        for machine in ("laptop", "dellserver"):
            m = next(x for x in cfg.machines if x.name == machine)
            assert "newrepo" in m.repos
            assert m.repo_path("newrepo") == "~/src/newrepo"

        assert [lbl for _, lbl in created] == ["coord", "tier:small", "tier:large"]

        # The residue is the point — it must name the steps it did NOT do.
        assert "NOT DONE" in result.output
        # #2299: the per-machine residue is a `git pull` of the settings
        # checkout, NOT an agent restart — an agent re-reads its own file, but
        # only the copy that is actually on its disk.
        assert "git pull" in result.output
        assert "RESTART coord-agent" not in result.output
        assert "CLAUDE.md" in result.output
        assert "pull_request" in result.output
        assert "coord repo doctor newrepo" in result.output

    def test_dry_run_writes_nothing(self, config_path, monkeypatch):
        monkeypatch.setattr(github_ops, "get_repo_default_branch", lambda repo: "main")
        monkeypatch.setattr(
            github_ops, "create_label",
            lambda *a, **k: pytest.fail("--dry-run must not touch GitHub"),
        )
        before = config_path.read_text()
        result = CliRunner().invoke(
            repo_add,
            [
                "newrepo", "--github", "acme/newrepo", "--machines", "laptop",
                "--config", str(config_path), "--dry-run",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert config_path.read_text() == before
        assert "would write" in result.output
        assert "name: newrepo" in result.output

    def test_refuses_a_repo_that_already_exists(self, config_path, monkeypatch):
        monkeypatch.setattr(github_ops, "get_repo_default_branch", lambda repo: "main")
        before = config_path.read_text()
        result = CliRunner().invoke(
            repo_add,
            ["api", "--github", "acme/api", "--config", str(config_path)],
        )
        assert result.exit_code != 0
        assert "already has a repos[] entry" in result.output
        assert config_path.read_text() == before

    def test_refuses_an_unknown_machine(self, config_path, monkeypatch):
        monkeypatch.setattr(github_ops, "get_repo_default_branch", lambda repo: "main")
        before = config_path.read_text()
        result = CliRunner().invoke(
            repo_add,
            [
                "newrepo", "--github", "acme/newrepo", "--machines", "ghost",
                "--config", str(config_path),
            ],
        )
        assert result.exit_code != 0
        assert "unknown machine" in result.output
        assert config_path.read_text() == before

    def test_refuses_when_the_default_branch_cannot_be_read(
        self, config_path, monkeypatch
    ):
        """Defaulting to `main` here is how worker PRs end up silently based on
        the wrong branch — the command must stop instead."""
        def _boom(repo):
            raise RuntimeError("gh: HTTP 401")

        monkeypatch.setattr(github_ops, "get_repo_default_branch", _boom)
        before = config_path.read_text()
        result = CliRunner().invoke(
            repo_add,
            ["newrepo", "--github", "acme/newrepo", "--config", str(config_path)],
        )
        assert result.exit_code != 0
        assert "default branch" in result.output
        assert "--default-branch" in result.output
        assert config_path.read_text() == before

    def test_explicit_default_branch_override_skips_the_github_read(
        self, config_path, monkeypatch
    ):
        monkeypatch.setattr(
            github_ops, "get_repo_default_branch",
            lambda repo: pytest.fail("--default-branch must skip the GitHub read"),
        )
        monkeypatch.setattr(github_ops, "create_label", lambda *a, **k: None)
        result = CliRunner().invoke(
            repo_add,
            [
                "newrepo", "--github", "acme/newrepo", "--config", str(config_path),
                "--default-branch", "trunk", "--machines", "laptop",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        from coord.config import load

        assert load(config_path).repo("newrepo").default_branch == "trunk"
        assert "NOT verified against GitHub" in result.output

    def test_no_labels_flag_makes_zero_label_calls(self, config_path, monkeypatch):
        monkeypatch.setattr(github_ops, "get_repo_default_branch", lambda repo: "main")
        monkeypatch.setattr(
            github_ops, "create_label",
            lambda *a, **k: pytest.fail("--no-labels must not create labels"),
        )
        result = CliRunner().invoke(
            repo_add,
            [
                "newrepo", "--github", "acme/newrepo", "--config", str(config_path),
                "--no-labels", "--machines", "laptop",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

    def test_label_failure_does_not_lose_the_config_write(
        self, config_path, monkeypatch
    ):
        """The config edit is the irreversible half; a `gh` hiccup on labels
        must degrade to a warning, not roll it back or crash."""
        monkeypatch.setattr(github_ops, "get_repo_default_branch", lambda repo: "main")

        def _boom(repo, label, **k):
            raise RuntimeError("gh: rate limited")

        monkeypatch.setattr(github_ops, "create_label", _boom)
        result = CliRunner().invoke(
            repo_add,
            [
                "newrepo", "--github", "acme/newrepo", "--config", str(config_path),
                "--machines", "laptop",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        from coord.config import load

        assert load(config_path).repo("newrepo") is not None
        assert "label creation failed" in result.output

    def test_refuses_without_a_coord_settings_checkout(self, monkeypatch, tmp_path):
        """#1832: `coord repo add` writes the TRACKED config so the change can
        be reviewed and pulled — never through the ~/.coord symlink."""
        monkeypatch.setenv("COORD_SETTINGS_DIR", str(tmp_path / "absent"))
        result = CliRunner().invoke(
            repo_add, ["newrepo", "--github", "acme/newrepo"]
        )
        assert result.exit_code != 0
        assert "coord-settings" in result.output

    def test_defaults_to_the_coord_settings_tracked_file(self, monkeypatch, tmp_path):
        settings = tmp_path / "coord-settings"
        (settings / "coord").mkdir(parents=True)
        tracked = settings / "coord" / "coordinator.yml"
        tracked.write_text(CONFIG)
        monkeypatch.setenv("COORD_SETTINGS_DIR", str(settings))
        monkeypatch.setattr(github_ops, "get_repo_default_branch", lambda repo: "main")
        monkeypatch.setattr(github_ops, "create_label", lambda *a, **k: None)

        result = CliRunner().invoke(
            repo_add,
            ["newrepo", "--github", "acme/newrepo", "--machines", "laptop"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert str(tracked) in result.output
        from coord.config import load

        assert load(tracked).repo("newrepo") is not None

    def test_residue_names_the_new_readiness_checks_as_next_actions(
        self, config_path, monkeypatch
    ):
        """#3073: `coord repo add` prints residue for what it deliberately
        did NOT do — the readiness checks it now leaves for `coord repo
        doctor` must be named as consequences, not left implicit."""
        monkeypatch.setattr(github_ops, "get_repo_default_branch", lambda repo: "main")
        monkeypatch.setattr(github_ops, "create_label", lambda *a, **k: None)

        result = CliRunner().invoke(
            repo_add,
            [
                "newrepo", "--github", "acme/newrepo", "--machines", "laptop",
                "--config", str(config_path),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "3073" in result.output
        assert "FAILS" in result.output
        assert "quality-check" in result.output
        assert "unreachable" in result.output


class TestDoctorWiring:
    """#2220: a half-onboarded repo shows up in the fleet report without
    anyone remembering to ask."""

    def test_coord_doctor_surfaces_the_skew(self, config_path, monkeypatch):
        from coord.commands.status import doctor
        from coord.config import load

        cfg = load(config_path)
        laptop, dellserver = cfg.machines
        probes = {
            "git": {"found": True, "version": "9.9", "capability": None, "ok": True},
            "gh": {"found": True, "version": "9.9", "capability": None, "ok": True},
            "python3": {
                "found": True, "version": "9.9", "capability": "python", "ok": True,
            },
        }
        statuses = []
        for m, repos in ((laptop, ["api"]), (dellserver, [])):
            st = _status(m, repos)
            st.health["tool_versions"] = probes
            statuses.append(st)
        monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: statuses)

        result = CliRunner().invoke(
            doctor,
            ["--config", str(config_path), "--no-pypi"],
            catch_exceptions=False,
        )
        assert result.exit_code == 1, result.output
        assert "repo onboarding" in result.output
        assert "machines.agent_repo_skew" in result.output
        assert "coord repo doctor <name>" in result.output

    def test_coord_doctor_stays_silent_on_a_healthy_fleet(
        self, config_path, monkeypatch
    ):
        from coord.commands.status import doctor
        from coord.config import load

        cfg = load(config_path)
        probes = {
            "git": {"found": True, "version": "9.9", "capability": None, "ok": True},
            "gh": {"found": True, "version": "9.9", "capability": None, "ok": True},
            "python3": {
                "found": True, "version": "9.9", "capability": "python", "ok": True,
            },
        }
        statuses = []
        for m in cfg.machines:
            st = _status(m, ["api"])
            st.health["tool_versions"] = probes
            statuses.append(st)
        monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: statuses)

        result = CliRunner().invoke(
            doctor,
            ["--config", str(config_path), "--no-pypi"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "repo onboarding" not in result.output
