"""Tests for the `acceptance:` block in coordinator.yml (#944, the oracle
loop runner + tui-tuidriver driver + sealing v1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import AcceptanceConfig, AcceptanceDriverConfig, ConfigError, load


BASE = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
"""


def test_acceptance_absent_defaults_to_empty(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.acceptance == AcceptanceConfig()
    assert cfg.acceptance.driver_for("coord-tui") is None


def test_acceptance_parses_driver(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test --test acceptance -- --format json"
      mock: "*.screen"
      capability: rust
"""
    )
    cfg = load(p)
    driver = cfg.acceptance.driver_for("coord-tui")
    assert driver == AcceptanceDriverConfig(
        kind="tui-tuidriver",
        run="cargo test --test acceptance -- --format json",
        mock="*.screen",
        capability="rust",
    )


def test_acceptance_driver_mock_and_capability_optional(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test --test acceptance"
"""
    )
    cfg = load(p)
    driver = cfg.acceptance.driver_for("coord-tui")
    assert driver.mock == ""
    assert driver.capability == ""


def test_acceptance_unconfigured_repo_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test"
"""
    )
    cfg = load(p)
    assert cfg.acceptance.driver_for("some-other-repo") is None


def test_acceptance_not_a_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "acceptance: [1, 2]\n")
    with pytest.raises(ConfigError, match="'acceptance' must be a mapping"):
        load(p)


def test_acceptance_drivers_not_a_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "acceptance:\n  drivers: [1, 2]\n")
    with pytest.raises(ConfigError, match="acceptance.drivers must be a mapping"):
        load(p)


def test_acceptance_driver_missing_kind_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      run: "cargo test"
"""
    )
    with pytest.raises(ConfigError, match="kind is required"):
        load(p)


def test_acceptance_driver_missing_run_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
"""
    )
    with pytest.raises(ConfigError, match="run is required"):
        load(p)


def test_acceptance_driver_entry_not_a_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui: "not-a-mapping"
"""
    )
    with pytest.raises(ConfigError, match="must be a mapping"):
        load(p)


def test_acceptance_driver_mock_non_string_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test"
      mock: [1, 2]
"""
    )
    with pytest.raises(ConfigError, match="mock must be a string"):
        load(p)


def test_acceptance_driver_capability_non_string_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test"
      capability: [1, 2]
"""
    )
    with pytest.raises(ConfigError, match="capability must be a string"):
        load(p)


# --- #1125: in-repo path routing -------------------------------------------

ROUTED_CONFIG = """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - match: "coord/dashboard/webapp/**"
          kind: web-playwright
          run: "cd coord/dashboard/webapp && npm run test:acceptance -- {ms}"
          mock: "*.html"
          capability: browser
        - match: "coord/**"
          kind: cli-pytest
          run: "pytest tests/acceptance/{ms}"
          mock: "*.out"
          capability: python
        - match: "tui/**"
          kind: tui-tuidriver
          run: "cargo test --test acceptance -- --format json"
          mock: "*.screen"
          capability: rust
"""


def _routed_cfg(tmp_path: Path):
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE.replace("coord-tui", "claude-coordinator") + ROUTED_CONFIG)
    return load(p)


def test_driver_for_routes_python_path_to_cli_pytest(tmp_path: Path) -> None:
    cfg = _routed_cfg(tmp_path)
    driver = cfg.acceptance.driver_for("claude-coordinator", "coord/acceptance.py")
    assert driver.kind == "cli-pytest"
    assert driver.match == "coord/**"
    assert driver.mock == "*.out"
    assert driver.capability == "python"


def test_driver_for_routes_rust_path_to_tui_tuidriver(tmp_path: Path) -> None:
    cfg = _routed_cfg(tmp_path)
    driver = cfg.acceptance.driver_for("claude-coordinator", "tui/src/app.rs")
    assert driver.kind == "tui-tuidriver"
    assert driver.match == "tui/**"
    assert driver.mock == "*.screen"
    assert driver.capability == "rust"


# ── #1540: coord/dashboard/webapp/** -> web-playwright + browser ─────────────
#
# A change under coord/dashboard/webapp/** used to match the coord/**
# route above and get handed to cli-pytest, which is wrong — the webapp's
# acceptance suite is Playwright and needs a browser-capable machine. The
# fix is ordering, not new resolution logic: driver_for() already resolves
# first-match-wins (#1125), so the webapp route just has to be listed before
# coord/** in ROUTED_CONFIG (verified above) — these tests pin that behavior
# so a future reorder or an accidental duplicate `coord/**`-only route
# regresses loudly instead of silently routing webapp changes to pytest.


def test_driver_for_routes_webapp_path_to_web_playwright(tmp_path: Path) -> None:
    cfg = _routed_cfg(tmp_path)
    driver = cfg.acceptance.driver_for(
        "claude-coordinator", "coord/dashboard/webapp/src/x.tsx"
    )
    assert driver.kind == "web-playwright"
    assert driver.match == "coord/dashboard/webapp/**"
    assert driver.mock == "*.html"
    assert driver.capability == "browser"


def test_driver_for_routes_webapp_route_declares_no_entrypoint(tmp_path: Path) -> None:
    # #1552 decision, made deliberately rather than by omission: Playwright
    # discovers specs by walking testDir (playwright.acceptance.config.ts),
    # exactly like cli-pytest's directory walk — there is no crate-root-style
    # file a slice must be wired into before it's reachable, so this route
    # has no `entrypoint:` and contributes nothing extra to sealed_paths().
    cfg = _routed_cfg(tmp_path)
    driver = cfg.acceptance.driver_for(
        "claude-coordinator", "coord/dashboard/webapp/src/x.tsx"
    )
    assert driver.entrypoint == ""
    assert cfg.acceptance.sealed_paths("claude-coordinator") == ["tests/acceptance/"]


def test_driver_for_routes_webapp_wins_over_coord_catchall(tmp_path: Path) -> None:
    # coord/dashboard/webapp/** is a strict subset of coord/** — this proves
    # the more specific route wins because it is ORDERED first, not because
    # driver_for() does any most-specific-match reasoning (it doesn't, see
    # test_driver_for_routes_first_match_wins below).
    cfg = _routed_cfg(tmp_path)
    driver = cfg.acceptance.driver_for(
        "claude-coordinator", "coord/dashboard/webapp/src/components/Panel.tsx"
    )
    assert driver.kind == "web-playwright"
    assert driver.kind != "cli-pytest"


def test_driver_for_routes_no_regression_python_path(tmp_path: Path) -> None:
    # A non-webapp coord/** path must still resolve to cli-pytest/python —
    # the new webapp route must not shadow the rest of coord/**.
    cfg = _routed_cfg(tmp_path)
    driver = cfg.acceptance.driver_for("claude-coordinator", "coord/state.py")
    assert driver.kind == "cli-pytest"
    assert driver.capability == "python"


def test_driver_for_routes_no_regression_rust_path(tmp_path: Path) -> None:
    cfg = _routed_cfg(tmp_path)
    driver = cfg.acceptance.driver_for("claude-coordinator", "tui/src/app.rs")
    assert driver.kind == "tui-tuidriver"
    assert driver.capability == "rust"


def test_driver_for_routes_first_match_wins(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - match: "**"
          kind: cli-pytest
          run: "pytest ."
        - match: "tui/**"
          kind: tui-tuidriver
          run: "cargo test"
"""
    )
    cfg = load(p)
    driver = cfg.acceptance.driver_for("claude-coordinator", "tui/src/app.rs")
    # The catch-all "**" is listed first, so it wins even though "tui/**"
    # would also match — first-match, not most-specific-match.
    assert driver.kind == "cli-pytest"


def test_driver_for_routes_no_match_returns_none(tmp_path: Path) -> None:
    cfg = _routed_cfg(tmp_path)
    assert cfg.acceptance.driver_for("claude-coordinator", "docs/README.md") is None


def test_driver_for_routes_without_path_returns_none(tmp_path: Path) -> None:
    cfg = _routed_cfg(tmp_path)
    # No path given -> can't select a route; not a guess.
    assert cfg.acceptance.driver_for("claude-coordinator") is None
    assert cfg.acceptance.driver_for("claude-coordinator", None) is None


def test_driver_for_no_routes_falls_back_to_flat_form_with_path(tmp_path: Path) -> None:
    # Back-compat: an existing flat (non-routed) config ignores `path`
    # entirely and returns its one driver, exactly like before #1125.
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test --test acceptance -- --format json"
"""
    )
    cfg = load(p)
    driver = cfg.acceptance.driver_for("coord-tui", "anything/at/all.py")
    assert driver.kind == "tui-tuidriver"


def test_acceptance_routes_empty_list_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes: []
"""
    )
    with pytest.raises(ConfigError, match="routes must be a non-empty list"):
        load(p)


def test_acceptance_route_missing_match_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - kind: cli-pytest
          run: "pytest ."
"""
    )
    with pytest.raises(ConfigError, match=r"routes\[0\]\.match is required"):
        load(p)


def test_acceptance_route_missing_kind_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - match: "coord/**"
          run: "pytest ."
"""
    )
    with pytest.raises(ConfigError, match=r"routes\[0\]\.kind is required"):
        load(p)


def test_acceptance_route_missing_run_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - match: "coord/**"
          kind: cli-pytest
"""
    )
    with pytest.raises(ConfigError, match=r"routes\[0\]\.run is required"):
        load(p)


def test_acceptance_route_entry_not_a_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - "not-a-mapping"
"""
    )
    with pytest.raises(ConfigError, match=r"routes\[0\] must be a mapping"):
        load(p)


def test_acceptance_routes_not_a_list_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes: "not-a-list"
"""
    )
    with pytest.raises(ConfigError, match="routes must be a non-empty list"):
        load(p)


# ── has_driver (#1125 review finding 1) ──────────────────────────────────────
#
# Path-independent "does this repo participate in the oracle loop at all"
# predicate — Gate A / sealing / briefing-injection call sites must not
# silently flip from "yes" to "no" the moment a repo's driver becomes routed.


def test_has_driver_false_for_unconfigured_repo(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.acceptance.has_driver("coord-tui") is False


def test_has_driver_true_for_flat_driver(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test"
"""
    )
    cfg = load(p)
    assert cfg.acceptance.has_driver("coord-tui") is True


def test_has_driver_true_for_routed_driver_with_no_path(tmp_path: Path) -> None:
    # The whole point of #1125 review finding 1: driver_for(repo) with no
    # path returns None for a routed repo (by design — it can't pick a
    # route), but has_driver must still say True, since the repo plainly
    # DOES participate in the oracle loop.
    cfg = _routed_cfg(tmp_path)
    assert cfg.acceptance.driver_for("claude-coordinator") is None
    assert cfg.acceptance.has_driver("claude-coordinator") is True


# ── double-config footgun (#1125 review finding 5) ───────────────────────────


def test_acceptance_routes_and_flat_kind_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      kind: cli-pytest
      routes:
        - match: "coord/**"
          kind: cli-pytest
          run: "pytest tests/acceptance/{ms}"
"""
    )
    with pytest.raises(ConfigError, match="sets both 'routes' and flat field"):
        load(p)


def test_acceptance_routes_and_flat_run_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      run: "pytest ."
      routes:
        - match: "coord/**"
          kind: cli-pytest
          run: "pytest tests/acceptance/{ms}"
"""
    )
    with pytest.raises(ConfigError, match="sets both 'routes' and flat field"):
        load(p)


# ── #1552: driver `entrypoint:` + the derived sealed set ─────────────────────
#
# Before this, `sealed_paths` was a single hardcoded literal
# (`tests/acceptance/`) in coord/review.py. That fits a directory-discovered
# suite (`pytest tests/acceptance/{ms}`) and is structurally unsatisfiable for
# an entry-point-linked one: `cargo test --test acceptance` cannot see a slice
# until `tui/tests/acceptance.rs` `include!`s it, and that file was outside the
# sealed set — so a `test-author` had to choose between a mandatory
# request-changes and shipping a slice that never runs. Each route now declares
# its own entry point.

ROUTED_WITH_ENTRYPOINT = """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - match: "coord/**"
          kind: cli-pytest
          run: "pytest tests/acceptance/{ms}"
        - match: "tui/**"
          kind: tui-tuidriver
          run: "cd tui && cargo test --test acceptance"
          entrypoint: "tui/tests/acceptance.rs"
"""


def test_route_entrypoint_parses(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE.replace("coord-tui", "claude-coordinator") + ROUTED_WITH_ENTRYPOINT)
    cfg = load(p)
    rust = cfg.acceptance.driver_for("claude-coordinator", "tui/src/app.rs")
    assert rust is not None
    assert rust.entrypoint == "tui/tests/acceptance.rs"
    py = cfg.acceptance.driver_for("claude-coordinator", "coord/review.py")
    assert py is not None
    assert py.entrypoint == ""


def test_flat_driver_entrypoint_parses(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test --test acceptance"
      entrypoint: "tests/acceptance.rs"
"""
    )
    cfg = load(p)
    assert cfg.acceptance.entrypoints("coord-tui") == ["tests/acceptance.rs"]
    assert cfg.acceptance.sealed_paths("coord-tui") == [
        "tests/acceptance/", "tests/acceptance.rs",
    ]


def test_entrypoints_are_path_independent_across_routes(tmp_path: Path) -> None:
    """Mirrors has_driver's rationale: sealing is a whole-repo question, so it
    must not depend on which route a given file resolves to."""
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE.replace("coord-tui", "claude-coordinator") + ROUTED_WITH_ENTRYPOINT)
    cfg = load(p)
    assert cfg.acceptance.entrypoints("claude-coordinator") == ["tui/tests/acceptance.rs"]
    # #2896: the entrypoint also seals its own sibling `acceptance/` dir —
    # where the tui-tuidriver route's relocated JIT-authored slices now live
    # (tui/tests/acceptance/ms-NN/*.rs) — alongside the repo-root tree the
    # cli-pytest route's ms-37 slices still use.
    assert cfg.acceptance.sealed_paths("claude-coordinator") == [
        "tests/acceptance/", "tui/tests/acceptance.rs", "tui/tests/acceptance/",
    ]


def test_sealed_paths_without_entrypoint_is_just_the_tree(tmp_path: Path) -> None:
    """Back-compat: the pytest route declares none, and #1175's rule is
    completely unchanged for it."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: cli-pytest
      run: "pytest tests/acceptance/{ms}"
"""
    )
    cfg = load(p)
    assert cfg.acceptance.entrypoints("coord-tui") == []
    assert cfg.acceptance.sealed_paths("coord-tui") == ["tests/acceptance/"]


def test_sealed_paths_empty_for_unconfigured_repo(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.acceptance.sealed_paths("coord-tui") == []
    assert cfg.acceptance.entrypoints("coord-tui") == []


# ── #2896: acceptance_search_roots — every root a bare milestone number
# could resolve under, for a caller with no single path in hand ───────────


def test_acceptance_search_roots_empty_for_unconfigured_repo(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.acceptance.acceptance_search_roots("coord-tui") == []


def test_acceptance_search_roots_without_entrypoint_is_just_the_tree(
    tmp_path: Path,
) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: cli-pytest
      run: "pytest tests/acceptance/{ms}"
"""
    )
    cfg = load(p)
    assert cfg.acceptance.acceptance_search_roots("coord-tui") == ["tests/acceptance/"]


def test_acceptance_search_roots_includes_entrypoint_sibling_dir(
    tmp_path: Path,
) -> None:
    """#2896: the directory ENTRIES of sealed_paths() — the entrypoint FILE
    itself is filtered out, since a caller searching for a milestone's
    manifest/contract wants directories to look under, not the crate-root
    file the driver links slices through."""
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE.replace("coord-tui", "claude-coordinator") + ROUTED_WITH_ENTRYPOINT)
    cfg = load(p)
    assert cfg.acceptance.acceptance_search_roots("claude-coordinator") == [
        "tests/acceptance/", "tui/tests/acceptance/",
    ]


def test_entrypoint_absolute_path_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test"
      entrypoint: "/home/john/src/coord-tui/tests/acceptance.rs"
"""
    )
    with pytest.raises(ConfigError, match="repo-root-relative"):
        load(p)


def test_entrypoint_directory_raises(tmp_path: Path) -> None:
    """A trailing slash would be read as a directory prefix by the sealed-set
    matcher, silently sealing something other than the entry point."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test"
      entrypoint: "tui/tests/"
"""
    )
    with pytest.raises(ConfigError, match="must name a FILE"):
        load(p)


def test_entrypoint_non_string_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test"
      entrypoint: 42
"""
    )
    with pytest.raises(ConfigError, match="must be a string"):
        load(p)


def test_routes_and_flat_entrypoint_raises(tmp_path: Path) -> None:
    """A routed entry's driver is entirely per-route (#1125 finding 5) — that
    now covers `entrypoint` too, or it would be silently discarded."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      entrypoint: "tui/tests/acceptance.rs"
      routes:
        - match: "tui/**"
          kind: tui-tuidriver
          run: "cargo test"
"""
    )
    with pytest.raises(ConfigError, match="sets both 'routes' and flat field"):
        load(p)


# ── #1733: driver `setup:` — the provisioning step ───────────────────────────
#
# `coord acceptance record`'s throwaway `git worktree add --detach` never has
# `node_modules` (gitignored, never checked out), so a JS driver's `run` alone
# fails with a bare `exit 127` (playwright not found) before ever producing a
# verdict. `setup` is an optional shell command run once, before `run`, to
# provision whatever a driver needs beyond a bare checkout — e.g. `npm ci`.

def test_acceptance_driver_setup_optional_defaults_empty(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test --test acceptance"
"""
    )
    cfg = load(p)
    driver = cfg.acceptance.driver_for("coord-tui")
    assert driver.setup == ""


def test_flat_driver_setup_parses(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: web-playwright
      run: "npx playwright test tests/acceptance/{ms}"
      setup: "npm ci"
"""
    )
    cfg = load(p)
    driver = cfg.acceptance.driver_for("coord-tui")
    assert driver.setup == "npm ci"


def test_route_setup_parses(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - match: "coord/dashboard/webapp/**"
          kind: web-playwright
          run: "cd coord/dashboard/webapp && npm run test:acceptance -- {ms}"
          setup: "cd coord/dashboard/webapp && npm ci"
        - match: "coord/**"
          kind: cli-pytest
          run: "pytest tests/acceptance/{ms}"
"""
    )
    cfg = load(p)
    webapp = cfg.acceptance.driver_for("claude-coordinator", "coord/dashboard/webapp/app.ts")
    assert webapp is not None
    assert webapp.setup == "cd coord/dashboard/webapp && npm ci"
    pyroute = cfg.acceptance.driver_for("claude-coordinator", "coord/review.py")
    assert pyroute is not None
    assert pyroute.setup == ""


def test_flat_driver_setup_non_string_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test"
      setup: 42
"""
    )
    with pytest.raises(ConfigError, match="setup must be a string"):
        load(p)


def test_route_setup_non_string_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - match: "coord/**"
          kind: cli-pytest
          run: "pytest tests/acceptance/{ms}"
          setup: true
"""
    )
    with pytest.raises(ConfigError, match="setup must be a string"):
        load(p)


def test_routes_and_flat_setup_raises(tmp_path: Path) -> None:
    """Mirrors test_routes_and_flat_entrypoint_raises — a routed entry's
    driver is entirely per-route (#1125 finding 5), now covering `setup`
    too, or it would be silently discarded."""
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      setup: "npm ci"
      routes:
        - match: "coord/**"
          kind: cli-pytest
          run: "pytest tests/acceptance/{ms}"
"""
    )
    with pytest.raises(ConfigError, match="sets both 'routes' and flat field"):
        load(p)
