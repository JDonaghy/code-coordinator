"""#2220: ``coord repo add`` edits the fleet's live ``coordinator.yml``, and
that file is mostly comments — each one the record of an incident.

A YAML round trip would delete every one of them. So :mod:`coord.repo_edit`
does line-level surgery, and these tests are what keep it honest: the edit must
land, the result must still parse, and **every original byte that was not the
edit must survive**.
"""

from __future__ import annotations

import pytest

from coord.config import load as load_config
from coord.repo_edit import (
    RepoEditError,
    add_repo_to_machine,
    insert_acceptance_driver_entry,
    insert_repo_entry,
    render_acceptance_driver_entry,
    render_repo_entry,
)


COMMENTED_CONFIG = """\
# Fleet config. Do not reformat — every comment below is an incident record.

repos:
  # api is the oldest entry; the depends_on here is load-bearing (#111).
  - name: api
    github: acme/api
    depends_on: [shared]
    default_branch: main
    test_command: "make test"

  - name: shared
    github: acme/shared
    depends_on: []
    default_branch: main

# Machines. Adding one here without also cloning the repo is #2220's whole
# subject.
machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [api, shared]
    repo_paths:
      api: ~/src/api
      shared: ~/src/shared

  # dellserver runs headless — no GTK.
  - name: dellserver
    host: dellserver.tailnet
    capabilities: [python]
    repos: [api]
    repo_paths:
      api: ~/src/api

# Concurrency settings
concurrency:
  max_workers: 3
"""


def _load(tmp_path, text, name="edited.yml"):
    p = tmp_path / name
    p.write_text(text)
    return load_config(p)


class TestInsertRepoEntry:
    def test_new_repo_parses_and_lands(self, tmp_path):
        entry = render_repo_entry("newrepo", "acme/newrepo", "develop")
        out = insert_repo_entry(COMMENTED_CONFIG, entry)
        cfg = _load(tmp_path, out)
        repo = cfg.repo("newrepo")
        assert repo is not None
        assert repo.github == "acme/newrepo"
        assert repo.default_branch == "develop"

    def test_every_comment_survives(self, tmp_path):
        entry = render_repo_entry("newrepo", "acme/newrepo", "main")
        out = insert_repo_entry(COMMENTED_CONFIG, entry)
        for line in COMMENTED_CONFIG.splitlines():
            if line.strip().startswith("#"):
                assert line in out, f"comment lost: {line!r}"

    def test_existing_repos_are_untouched(self, tmp_path):
        entry = render_repo_entry("newrepo", "acme/newrepo", "main")
        cfg = _load(tmp_path, insert_repo_entry(COMMENTED_CONFIG, entry))
        api = cfg.repo("api")
        assert api.depends_on == ["shared"]
        assert api.test_command == "make test"

    def test_the_entry_lands_inside_repos_not_after_the_next_section(self, tmp_path):
        """The failure mode this guards: appending past the `# Concurrency
        settings` comment, which parses fine and silently drops the repo."""
        entry = render_repo_entry("newrepo", "acme/newrepo", "main")
        out = insert_repo_entry(COMMENTED_CONFIG, entry)
        assert out.index("name: newrepo") < out.index("machines:")
        assert out.index("# Concurrency settings") > out.index("name: newrepo")

    def test_config_without_a_repos_block_is_refused_not_guessed(self):
        with pytest.raises(RepoEditError, match="repos"):
            insert_repo_entry("machines: []\n", render_repo_entry("r", "a/r", "main"))

    def test_optional_commands_are_rendered_when_given(self, tmp_path):
        entry = render_repo_entry(
            "newrepo", "acme/newrepo", "main",
            build_command="cargo build", test_command="cargo test",
        )
        cfg = _load(tmp_path, insert_repo_entry(COMMENTED_CONFIG, entry))
        repo = cfg.repo("newrepo")
        assert repo.build_command == "cargo build"
        assert repo.test_command == "cargo test"

    def test_omitted_commands_are_omitted_not_guessed(self, tmp_path):
        """A guessed `test_command` produces a Test stage that runs the wrong
        suite and calls it green — strictly worse than an unresolved one, which
        `coord repo doctor` reports as a CRIT."""
        entry = render_repo_entry("newrepo", "acme/newrepo", "main")
        assert "test_command" not in entry
        assert "build_command" not in entry


class TestAddRepoToMachine:
    def test_inline_flow_list_gains_the_repo_and_a_path(self, tmp_path):
        entry = render_repo_entry("newrepo", "acme/newrepo", "main")
        out = insert_repo_entry(COMMENTED_CONFIG, entry)
        out = add_repo_to_machine(out, "dellserver", "newrepo", "~/src/newrepo")
        cfg = _load(tmp_path, out)
        dell = next(m for m in cfg.machines if m.name == "dellserver")
        assert "newrepo" in dell.repos
        assert dell.repo_path("newrepo") == "~/src/newrepo"
        # The other machine is untouched.
        laptop = next(m for m in cfg.machines if m.name == "laptop")
        assert "newrepo" not in laptop.repos

    def test_two_machines_in_sequence(self, tmp_path):
        out = insert_repo_entry(
            COMMENTED_CONFIG, render_repo_entry("newrepo", "acme/newrepo", "main")
        )
        for machine in ("laptop", "dellserver"):
            out = add_repo_to_machine(out, machine, "newrepo", "~/src/newrepo")
        cfg = _load(tmp_path, out)
        for machine in ("laptop", "dellserver"):
            m = next(x for x in cfg.machines if x.name == machine)
            assert "newrepo" in m.repos
            assert m.repo_path("newrepo") == "~/src/newrepo"

    def test_is_idempotent(self, tmp_path):
        out = insert_repo_entry(
            COMMENTED_CONFIG, render_repo_entry("newrepo", "acme/newrepo", "main")
        )
        once = add_repo_to_machine(out, "laptop", "newrepo", "~/src/newrepo")
        twice = add_repo_to_machine(once, "laptop", "newrepo", "~/src/newrepo")
        assert once == twice
        cfg = _load(tmp_path, twice)
        laptop = next(m for m in cfg.machines if m.name == "laptop")
        assert laptop.repos.count("newrepo") == 1

    def test_comments_inside_machines_survive(self):
        out = insert_repo_entry(
            COMMENTED_CONFIG, render_repo_entry("newrepo", "acme/newrepo", "main")
        )
        out = add_repo_to_machine(out, "dellserver", "newrepo", "~/src/newrepo")
        assert "# dellserver runs headless — no GTK." in out
        assert "# Concurrency settings" in out

    def test_unknown_machine_is_refused(self):
        with pytest.raises(RepoEditError, match="ghost"):
            add_repo_to_machine(COMMENTED_CONFIG, "ghost", "newrepo", "~/src/newrepo")

    def test_block_style_repos_list(self, tmp_path):
        block_cfg = """\
repos:
  - name: api
    github: acme/api

machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos:
      - api
    repo_paths:
      api: ~/src/api
"""
        out = insert_repo_entry(
            block_cfg, render_repo_entry("newrepo", "acme/newrepo", "main")
        )
        out = add_repo_to_machine(out, "laptop", "newrepo", "~/src/newrepo")
        cfg = _load(tmp_path, out)
        laptop = cfg.machines[0]
        assert laptop.repos == ["api", "newrepo"]
        assert laptop.repo_path("newrepo") == "~/src/newrepo"

    def test_machine_with_no_repo_paths_gains_the_key(self, tmp_path):
        """A machine with `repos:` but no `repo_paths:` entry is exactly the
        #1801 dispatch blocker — adding the key is a fix, not an assumption."""
        no_paths = """\
repos:
  - name: api
    github: acme/api

machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [api]
    repo_paths:
      api: ~/src/api
  - name: extra
    host: extra.tailnet
    capabilities: [python]
    repos: [api]
    repo_paths:
      api: ~/src/api
"""
        out = insert_repo_entry(
            no_paths, render_repo_entry("newrepo", "acme/newrepo", "main")
        )
        out = add_repo_to_machine(out, "extra", "newrepo", "/opt/newrepo")
        cfg = _load(tmp_path, out)
        extra = next(m for m in cfg.machines if m.name == "extra")
        assert extra.repo_path("newrepo") == "/opt/newrepo"


# ── #2748 (IL-2): acceptance.drivers.<repo> ──────────────────────────────────


CONFIG_WITH_ACCEPTANCE = """\
repos:
  - name: api
    github: acme/api
    depends_on: []
    default_branch: main

machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: ~/src/api

# Gate A / oracle-loop drivers.
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test --test acceptance -- --format json"
      mock: "*.screen"
      capability: rust

# Concurrency settings
concurrency:
  max_workers: 3
"""

CONFIG_NO_ACCEPTANCE = """\
repos:
  - name: api
    github: acme/api
    depends_on: []
    default_branch: main

machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: ~/src/api
"""


class TestAcceptanceDriverEntry:
    def test_appends_to_an_existing_drivers_block_and_preserves_comments(
        self, tmp_path
    ):
        entry = render_acceptance_driver_entry(
            "webapp", "web-playwright",
            "npx playwright test tests/acceptance/{ms}",
            setup="npm ci", mock="*.html", capability="browser",
        )
        out = insert_acceptance_driver_entry(CONFIG_WITH_ACCEPTANCE, entry)

        # Every original byte survives — the whole point of line-level
        # surgery over a YAML round trip.
        for line in CONFIG_WITH_ACCEPTANCE.splitlines():
            assert line in out
        assert "# Gate A / oracle-loop drivers." in out
        assert "# Concurrency settings" in out

        cfg = _load(tmp_path, out)
        assert sorted(cfg.acceptance.drivers) == ["coord-tui", "webapp"]
        webapp = cfg.acceptance.drivers["webapp"]
        assert webapp.kind == "web-playwright"
        assert webapp.setup == "npm ci"
        assert webapp.capability == "browser"
        # The pre-existing driver must be untouched.
        assert cfg.acceptance.drivers["coord-tui"].kind == "tui-tuidriver"
        # concurrency: after acceptance: must still parse.
        assert cfg.concurrency is not None

    def test_creates_the_acceptance_block_from_scratch(self, tmp_path):
        """Most of this project's history predates `acceptance:` — a fleet
        that never touched the oracle loop has no top-level key at all
        (mirrors tests/test_repo_add.py's own fixture)."""
        entry = render_acceptance_driver_entry(
            "grocery", "cli-pytest", "pytest tests/acceptance/{ms}",
            mock="*.out", capability="python",
        )
        out = insert_acceptance_driver_entry(CONFIG_NO_ACCEPTANCE, entry)
        for line in CONFIG_NO_ACCEPTANCE.splitlines():
            assert line in out

        cfg = _load(tmp_path, out)
        assert list(cfg.acceptance.drivers) == ["grocery"]
        grocery = cfg.acceptance.drivers["grocery"]
        assert grocery.kind == "cli-pytest"
        assert grocery.run == "pytest tests/acceptance/{ms}"
        assert grocery.mock == "*.out"
        assert grocery.capability == "python"

    def test_entrypoint_field_round_trips(self, tmp_path):
        entry = render_acceptance_driver_entry(
            "tui-app", "tui-tuidriver",
            "cargo test --test acceptance -- --format json",
            entrypoint="tui/tests/acceptance.rs", mock="*.screen",
        )
        out = insert_acceptance_driver_entry(CONFIG_NO_ACCEPTANCE, entry)
        cfg = _load(tmp_path, out)
        assert cfg.acceptance.drivers["tui-app"].entrypoint == "tui/tests/acceptance.rs"

    def test_creates_the_drivers_child_when_acceptance_exists_but_is_childless(
        self, tmp_path
    ):
        config = CONFIG_NO_ACCEPTANCE + "\nacceptance:\n  capability: []\n"
        entry = render_acceptance_driver_entry(
            "grocery", "cli-pytest", "pytest tests/acceptance/{ms}",
        )
        out = insert_acceptance_driver_entry(config, entry)
        cfg = _load(tmp_path, out)
        assert list(cfg.acceptance.drivers) == ["grocery"]
