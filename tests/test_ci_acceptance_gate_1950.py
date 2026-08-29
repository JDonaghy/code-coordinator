"""#1950: the sealed oracle-loop acceptance suite (tests/acceptance/ms-NN/)
was run by no automatic gate anywhere — `coord acceptance run`/`record` only
fires while a milestone is actively being driven, so a slice that goes red
after its milestone closes (ms-51 at #1547, silently, through
#1548/#1550/#1551/#1818) is never re-checked.

This is the regression guard for the fix: each of this repo's sealed routes
must be wired to a workflow that runs it whenever anything it covers changes.
Parsing the real workflow files — not fixtures — means these tests fail the
moment someone removes/disables/neuters a gate, which is exactly the
silent-regression class #1950 reported (the evidence in that issue was "no
workflow in .github/workflows/ references test:acceptance" — grep, not
judgement).

#2180 update: a route's CI step no longer runs its raw driver command
directly — it runs THROUGH `coord acceptance run --all --ci` (#2164), which
shells out to the same underlying command via
`.github/coord-ci-acceptance.yml`. The regression guarded against is
unchanged (the sealed suite silently stops running); the needle just moves to
the wrapper invocation, and `--all --ci` is pinned specifically because
#2180's acceptance criteria require it ("through coord acceptance run --all
--ci, not the raw driver command").

#2389 update: the web-playwright `acceptance` job moved out of `test.yml`
into its own `paths:`-gated `acceptance-web.yml`.

#2009 update (epic #2002) — THE WEB-PLAYWRIGHT HALF OF THIS FILE MOVED, IT
WAS NOT DELETED. The webapp left this repo for `coord-web`, taking ms-51 and
its Playwright config with it (docs/ADR_COORD_WEB_ACCEPTANCE_SUITE.md,
#2007), so `acceptance-web.yml`, the `e2e` job, and the
`coord/dashboard/webapp/**` route in `.github/coord-ci-acceptance.yml` are
all gone from here. Every assertion that parsed them (browser install,
Playwright cache, install retries, bounded timeout, paths gating) is now
`coord-web`'s CI's to make about `coord-web`'s CI, where the files those
assertions describe actually live.

#2899 update — THE TUI-TUIDRIVER HALF OF THIS FILE MOVED TOO, ON EXACTLY THE
#2009 PRECEDENT ABOVE. The `coord-tui` crate left this repo for its own
`coord-tui` repo, taking `tests/acceptance.rs`, the ms-33/38/65/67 slices and
`cargo-test.yml` with it. So `.github/workflows/cargo-test.yml` and the
`tui/**` route in `.github/coord-ci-acceptance.yml` are gone from here, and
every assertion that parsed them is coord-tui's CI's to make about coord-tui's
CI — where the files those assertions describe actually live. This repo's
scaffold for that is `scripts/coord-tui-scaffold/.github/`, which the
`test_the_coord_tui_scaffold_*` guards below check on its way out the door,
since it is the only moment this repo can.

What this file keeps is everything #1950 protects that is still IN this
repo — the cli-pytest route — plus the guards that stop either deletion from
being half-undone into the worst state of all: a job or route that looks like
a gate, is named like a gate, and covers a path that cannot appear in a diff.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test.yml"
CI_ACCEPTANCE_CONFIG_PATH = REPO_ROOT / ".github" / "coord-ci-acceptance.yml"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
#: #2899: the new repo's CI, staged here until the extraction script pushes it.
SCAFFOLD_DIR = REPO_ROOT / "scripts" / "coord-tui-scaffold"


def _load_workflow(path: Path = WORKFLOW_PATH) -> dict:
    # YAML's `on:` key parses to the boolean `True` under the default
    # SafeLoader (YAML 1.1 treats the bare word `on` as a boolean) — harmless
    # here since this test never reads that key, but noted so a future
    # reader isn't surprised the loaded dict's key isn't the string "on".
    return yaml.safe_load(path.read_text())


def _job_steps(workflow: dict, job_name: str, path: Path = WORKFLOW_PATH) -> list[dict]:
    jobs = workflow.get("jobs") or {}
    job = jobs.get(job_name)
    assert job is not None, (
        f"expected a {job_name!r} job in {path} — jobs present: "
        f"{sorted(jobs)}"
    )
    return job.get("steps") or []


def _step_runs(steps: list[dict], needle: str) -> dict | None:
    for step in steps:
        run = step.get("run")
        if isinstance(run, str) and needle in run:
            return step
    return None


def test_cli_pytest_route_runs_through_the_2164_ci_wrapper() -> None:
    """The cli-pytest sealed route (ms-37) is the #1950 guard that stayed in
    this repo. `--all --ci` is #2180's acceptance contract: the raw `pytest
    tests/acceptance` it wraps would never honour a manifest's `expected_red:`
    registry, so a slice authored red by design would fail the job exactly
    like a real regression and force `--force-merge` (#2164's whole reason
    for existing)."""
    workflow = _load_workflow()
    steps = _job_steps(workflow, "test")
    test_step = _step_runs(steps, "coord acceptance run")
    assert test_step is not None, (
        "no step in the 'test' job runs `coord acceptance run` — this is the "
        "exact bug #1950 reported (the sealed suite run by no automatic gate "
        "anywhere)"
    )
    run = test_step["run"]
    assert "--all" in run, f"acceptance step doesn't pass --all: {run!r}"
    assert "--ci" in run, f"acceptance step doesn't pass --ci: {run!r}"
    assert "--repo claude-coordinator" in run, (
        f"acceptance step doesn't scope to this repo: {run!r}"
    )
    assert test_step.get("continue-on-error") is not True, (
        "the `coord acceptance run` step has continue-on-error: true — a red "
        "sealed suite would no longer fail the job, and a gate that cannot "
        "fail is not a gate (#1950 item 3)"
    )


def test_ci_acceptance_config_resolves_the_remaining_route() -> None:
    """.github/coord-ci-acceptance.yml is what every `coord acceptance run
    --all --ci` step in CI points `--config` at (the real
    ~/.coord/coordinator.yml is outside this repo and unreachable from a
    runner). Parse it with the SAME loader coord.cli uses, and confirm the
    one remaining in-repo route (#1125: coord/** -> cli-pytest) resolves — a
    typo'd `match:` glob or a missing route would silently make some CI
    step's `--for-path` resolve to nothing (a loud `sys.exit(1)`, but only
    ever discovered by watching that job actually go red in CI, not by this
    faster/local check).

    #2899: the `tui/** -> tui-tuidriver` half of this assertion moved to
    coord-tui. See `test_ci_acceptance_config_has_no_tui_route` below for
    the guard that replaced it here.
    """
    assert CI_ACCEPTANCE_CONFIG_PATH.exists(), (
        f"{CI_ACCEPTANCE_CONFIG_PATH} is missing — every `coord acceptance "
        "run --all --ci --config .github/coord-ci-acceptance.yml` step in "
        "CI would fail to even load a config"
    )
    from coord.config import load as load_coord_config

    cfg = load_coord_config(str(CI_ACCEPTANCE_CONFIG_PATH))
    cli = cfg.acceptance.driver_for("claude-coordinator", "coord/cli.py")
    assert cli is not None and cli.kind == "cli-pytest"
    # #2164's --all always resolves the `{ms}` template to None (scope="all"
    # has no single milestone to substitute) — render_run_command leaves an
    # unreferenced `{ms}` LITERAL in that case rather than stripping it, so
    # a route written for --issue scoping (like the fleet's real
    # `pytest tests/acceptance/{ms}`) would try to collect a path literally
    # named `tests/acceptance/{ms}` here and crash outright. Every route in
    # this CI-only config must be written to already cover its whole
    # accumulated suite without needing a substitution.
    assert "{ms}" not in cli.run, (
        f"{cli.kind} route's run command still references {{ms}}, "
        f"which --all leaves unsubstituted and literal: {cli.run!r}"
    )


def test_ci_acceptance_config_has_no_tui_route() -> None:
    """#2899: `tui/**` cannot appear in a diff against this repo any more.

    Same failure shape as the webapp guard below, and the same reason it is
    asserted rather than assumed: `driver_for()` is FIRST-match (#1540), so a
    re-added tui route sits above cli-pytest and shadows it for any path it
    matches — while its `run:` command `cd`s into a directory this repo does
    not have. That is a CI step failing for a reason with nothing to do with
    the change under test.
    """
    from coord.config import load as load_coord_config

    cfg = load_coord_config(str(CI_ACCEPTANCE_CONFIG_PATH))
    for probe in ("tui/src/main.rs", "tui/tests/acceptance.rs"):
        resolved = cfg.acceptance.driver_for("claude-coordinator", probe)
        assert resolved is None or resolved.kind != "tui-tuidriver", (
            "a tui-tuidriver route is back in .github/coord-ci-acceptance.yml, "
            f"matching {probe} — a path this repo cannot contain (#2899)"
        )


def test_this_repos_sealed_set_is_exactly_the_acceptance_dir() -> None:
    """#2899: with the tui-tuidriver entrypoint gone, this repo's sealed set
    collapses back to `tests/acceptance/`.

    Worth pinning because the sealed set is what `coord/review.py` turns into
    an unconditional `request-changes`, and it is DERIVED from the configured
    driver `entrypoint:` rather than hardcoded — so it followed the crate
    automatically, and would just as automatically widen again if a route
    with an `entrypoint:` outside `tests/acceptance/` came back.
    """
    from coord.config import load as load_coord_config

    cfg = load_coord_config(str(CI_ACCEPTANCE_CONFIG_PATH))
    sealed = set(cfg.acceptance.sealed_paths("claude-coordinator"))
    assert sealed == {"tests/acceptance/"}, sealed
    assert not any(p.startswith("tui/") for p in sealed)


def test_the_coord_tui_scaffold_carries_the_gates_that_left_this_repo() -> None:
    """#2899: the tui-tuidriver CI gate did not disappear, it moved.

    This repo can only assert that once — at the moment the scaffold is
    staged here, before `scripts/extract-coord-tui.sh` pushes it into the new
    repo. After that it is coord-tui's CI's own business. Skipping the check
    entirely, though, is how a "move" quietly becomes a "delete": the whole
    point of #1950 is that a gate nobody runs looks exactly like a gate that
    passes.
    """
    cargo_wf = SCAFFOLD_DIR / ".github" / "workflows" / "cargo-test.yml"
    assert cargo_wf.exists(), f"{cargo_wf} missing — the moved Rust gate has no home"
    workflow = yaml.safe_load(cargo_wf.read_text())
    steps = _job_steps(workflow, "cargo-test")

    plain = _step_runs(steps, "cargo test")
    assert plain is not None, "the scaffolded workflow runs no `cargo test` at all"

    acc = _step_runs(steps, "coord acceptance run")
    assert acc is not None, (
        "no step in the scaffolded cargo-test.yml runs `coord acceptance "
        "run` — the tui-tuidriver sealed route would be unreachable from "
        "coord-tui's CI, i.e. the move lost the gate"
    )
    run = acc["run"]
    assert "--all" in run and "--ci" in run, (
        f"the scaffolded acceptance step doesn't use the #2164 CI wrapper "
        f"contract: {run!r}"
    )
    assert "--repo coord-tui" in run, (
        f"the scaffolded acceptance step still names the old repo: {run!r}"
    )


def test_the_coord_tui_scaffold_has_a_root_relative_sealed_entrypoint() -> None:
    """#2899: the crate is at the new repo's ROOT, so every path in its CI
    config must have shed the `tui/` prefix — including the driver
    `entrypoint:` the sealed set is derived from.

    A leftover prefix here is the quietest possible failure: the config still
    parses, the driver still resolves, and the sealed set names a file that
    does not exist — so the oracle-tamper gate protects nothing.
    """
    from coord.config import load as load_coord_config

    cfg_path = SCAFFOLD_DIR / ".github" / "coord-ci-acceptance.yml"
    assert cfg_path.exists(), f"{cfg_path} missing"
    cfg = load_coord_config(str(cfg_path))

    driver = cfg.acceptance.driver_for("coord-tui", "src/main.rs")
    assert driver is not None and driver.kind == "tui-tuidriver"
    assert "{ms}" not in driver.run
    assert "cd tui" not in driver.run, driver.run

    sealed = set(cfg.acceptance.sealed_paths("coord-tui"))
    assert sealed == {"tests/acceptance.rs", "tests/acceptance/"}, sealed
    assert not any(p.startswith("tui/") for p in sealed)


def test_ci_acceptance_config_has_no_webapp_route() -> None:
    """#2009: `coord/dashboard/webapp/**` cannot appear in a diff against
    this repo any more, so a route matching it is worse than useless.

    `driver_for()` is FIRST-match, not most-specific-match (#1540), and that
    glob is a strict subset of `coord/**` — so a re-added webapp route sits
    above the cli-pytest one and shadows it for any path it matches. Combined
    with a `run:` command that `cd`s into a directory this repo no longer
    has, the failure would be a CI step exiting non-zero for a reason with
    nothing to do with the change under test."""
    from coord.config import load as load_coord_config

    cfg = load_coord_config(str(CI_ACCEPTANCE_CONFIG_PATH))
    resolved = cfg.acceptance.driver_for(
        "claude-coordinator", "coord/dashboard/webapp/src/App.tsx"
    )
    # It may resolve (coord/** matches that path too) — it must simply not
    # resolve to the web driver, i.e. no webapp-specific route survives.
    assert resolved is None or resolved.kind != "web-playwright", (
        "a web-playwright route is back in .github/coord-ci-acceptance.yml, "
        "shadowing coord/** for paths this repo cannot contain (#2009)"
    )


def test_no_workflow_gates_a_webapp_route_that_no_longer_exists() -> None:
    """#2009: no workflow may be `paths:`-gated to, or `cd` into, the deleted
    webapp tree.

    A `paths:` filter naming a path that can never change is a job that never
    runs — indistinguishable in the Actions UI from a job that runs and
    passes, which is precisely the "green screenshot" shape #1950 is about.
    Asserted over EVERY workflow file rather than a named list, so a new one
    can't reintroduce it."""
    offenders = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            # Skip prose: several workflows explain the removal on purpose.
            if line.lstrip().startswith("#"):
                continue
            if "coord/dashboard/webapp" in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "workflow(s) still reference the deleted webapp tree (#2009):\n"
        + "\n".join(offenders)
    )


def test_non_acceptance_test_job_excludes_the_sealed_suite() -> None:
    """#2180's split: the ordinary `test` job's plain `pytest` must not also
    sweep up tests/acceptance/ms-37's .py files — that would make a
    red-by-design slice (one listed in some ms-NN/manifest.yml's
    expected_red:) fail this job exactly like an ordinary regression, for
    every PR in the repo, defeating the entire point of the #2164 wrapper
    this file's other tests confirm is wired in elsewhere."""
    workflow = _load_workflow()
    steps = _job_steps(workflow, "test")
    pytest_step = _step_runs(steps, "pytest")
    assert pytest_step is not None
    run = pytest_step["run"]
    assert "--ignore=tests/acceptance" in run, (
        f"the 'test' job's pytest step doesn't exclude tests/acceptance: {run!r}"
    )


def test_no_workflow_gates_a_tui_path_that_no_longer_exists() -> None:
    """#2899, mirroring the webapp guard above: no workflow in THIS repo may
    be `paths:`-gated to, or `cd` into, the moved `tui/` tree.

    A `paths:` filter naming a path that can never change is a job that never
    runs — indistinguishable in the Actions UI from a job that runs and
    passes. Asserted over EVERY workflow file rather than a named list, and
    skipping comment lines, since several of them explain the move on
    purpose."""
    offenders = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if "tui/" in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "workflow(s) still reference the moved tui/ tree (#2899):\n"
        + "\n".join(offenders)
    )


def test_the_cargo_test_workflow_is_gone_from_this_repo() -> None:
    """#2899: `.github/workflows/cargo-test.yml` moved to coord-tui.

    Left here it would be strictly worse than absent: `paths:`-gated on
    `tui/**`, so it never triggers, while still appearing in the Actions UI
    as a configured Rust gate for a repo with no Rust in it.
    """
    assert not (WORKFLOWS_DIR / "cargo-test.yml").exists(), (
        "cargo-test.yml is back in this repo — it gates a tree that moved to "
        "coord-tui (#2899), so it can only ever be a gate that never runs"
    )
