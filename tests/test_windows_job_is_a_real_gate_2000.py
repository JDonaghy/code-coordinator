"""#2000: the `windows` job in .github/workflows/test.yml is a REAL gate now.

    #3394 (2026-09-18) suspended the job from automatic CI — it is
    `workflow_dispatch`-only until Windows is active work again. Read
    `test_windows_job_is_never_automatic` below for what that changed and
    what it deliberately did not. The rest of this file is UNCHANGED and
    still load-bearing: the `continue-on-error` assertions guard the job's
    shape for whenever it runs, and `test_tzdata_is_a_base_dependency_...`
    guards a *production* defect that has nothing to do with CI scheduling.
    Suspending the job must not weaken either, which is why this file was
    re-pointed rather than deleted.

#1895 (CP-4) added the job, but it died during *collection* on Windows:

    ERROR tests/test_failure_class.py - zoneinfo._common.ZoneInfoNotFoundError:
          No time zone found with key America/Chicago

Windows ships no IANA time-zone database, so `zoneinfo` cannot resolve a named
zone unless the `tzdata` PyPI package is installed (it is picked up
automatically when present). That is NOT a CI-only concern: coord/config.py,
coord/models.py, coord/failure_class.py and coord/machine_pause.py all import
`zoneinfo` and resolve named zones on ordinary code paths, so a Windows user
running `pip install code-coordinator` hit the same error. Fixing it with a
`pip install tzdata` line in the workflow would have made the job green while
leaving the defect shipped — and hidden it from the very job that exists to
catch it. So it lives in the base `[project] dependencies` behind a
`sys_platform == 'win32'` marker (pinned by tests/test_client_base_install.py).

Because #1156 (CP-1, the import guards) had already killed the other known
Windows failure (`coord/interactive.py`'s unconditional `import fcntl`), the
tzdata fix was the last thing standing between this job and a genuine pass —
which is why the same change removed the `continue-on-error: true` the job's
own comment named #1156 as the exit condition for.

This test is the regression guard for the *second* half. The first half has a
test already; without this one, someone re-adding `continue-on-error` (or
reintroducing the historical job-level variant from #2026) silently downgrades
the job back to a green screenshot — the same "a gate that has never failed is
not a gate" failure mode tests/test_ci_acceptance_gate_1950.py guards for the
`acceptance` job.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test.yml"


def _load_workflow() -> dict:
    # YAML 1.1 parses a bare `on:` key as the boolean True under SafeLoader.
    # Harmless here (this test never reads it), but noted so a future reader
    # isn't surprised the key isn't the string "on".
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _windows_job() -> dict:
    jobs = _load_workflow().get("jobs") or {}
    job = jobs.get("windows")
    assert job is not None, (
        f"expected a 'windows' job in {WORKFLOW_PATH} — jobs present: "
        f"{sorted(jobs)}. #1895 added real Windows verification; if it is "
        "gone, nothing checks that coord works on Windows at all."
    )
    return job


def test_windows_job_runs_the_suite() -> None:
    steps = _windows_job().get("steps") or []
    runs = [s.get("run") for s in steps if isinstance(s.get("run"), str)]
    assert any("pytest" in r for r in runs), (
        "no step in the 'windows' job runs pytest — the job's whole point "
        f"is running the suite on windows-latest. Steps: {runs}"
    )


def test_windows_job_does_not_neuter_a_red_result() -> None:
    """No `continue-on-error`, at job level or on any step (#2000).

    Job level is called out specifically because #2026 found it is *worse*
    than useless: it softens the workflow-run conclusion while the `windows`
    **check** still reports `failure` to the Checks API, and
    `coord.ci_store._PASSING_CONCLUSIONS` is {success, skipped, neutral} with
    no notion of a non-blocking check.
    """
    job = _windows_job()
    assert job.get("continue-on-error") is not True, (
        "the 'windows' job has continue-on-error: true at the JOB level — "
        "#2026: that does not even do what it looks like (the check still "
        "reports failure to the Checks API, which is what stalled the drive "
        "queue on 2026-08-08), and #2000 removed it because the job passes"
    )
    for index, step in enumerate(job.get("steps") or []):
        assert step.get("continue-on-error") is not True, (
            f"step {index} ({step.get('run') or step.get('uses')!r}) of the "
            "'windows' job has continue-on-error: true — #1156 and #2000 "
            "both landed, so the job is expected to pass; swallowing its "
            "result turns a real gate back into a green screenshot"
        )


def test_windows_job_is_never_automatic() -> None:
    """The `if:` guard is what keeps this job off the critical path.

    #2039 originally made it push-only: on `pull_request` the job is skipped,
    and `skipped` is in `ci_store._PASSING_CONCLUSIONS`, so a red Windows run
    surfaced on main without blocking every open PR in the repo.

    #3394 narrowed it further to `workflow_dispatch` only. The job had failed
    every run sampled back to 2026-09-10 (~614 assorted errors), so push-only
    still meant a permanently red `main` — and a permanently red `main` makes
    coord mark every branch baseline-red and SKIP its test stage while the
    gate still reports `test: passed`. That is a worse outcome than no signal,
    because it is an *inverted* one.

    Both shapes satisfy the property this test actually defends: **the
    `windows` job never runs automatically on a `pull_request`.** Either
    literal is accepted, so restoring #2039's push-only form (the documented
    restore path, once the failures are fixed) does not require touching this
    test. What is rejected is the job becoming per-PR, or losing its guard
    altogether — that is the repo-wide merge block #2039 exists to prevent.
    """
    condition = _windows_job().get("if")
    assert isinstance(condition, str), (
        "the 'windows' job lost its `if:` guard entirely, so it now runs on "
        "every event including `pull_request`. Combined with #2000's "
        "continue-on-error removal, that makes a ~21-28 min Windows job a "
        "per-PR merge blocker (see #2039)."
    )
    accepted = ("workflow_dispatch", "push")
    assert any(event in condition for event in accepted), (
        f"the 'windows' job's `if:` is {condition!r}, which is neither "
        "#3394's `github.event_name == 'workflow_dispatch'` (current) nor "
        "#2039's `github.event_name == 'push'` (the restore target). One of "
        "those two is required — anything else risks per-PR blocking."
    )
    assert "pull_request" not in condition, (
        f"the 'windows' job's `if:` is {condition!r}, which admits "
        "`pull_request` events. #2039: a ~21-28 min Windows job on every PR, "
        "with no continue-on-error, blocks the whole repo on one regression."
    )


def test_tzdata_is_a_base_dependency_marked_for_win32() -> None:
    """The production-side half of #2000, asserted on the raw text.

    tests/test_client_base_install.py already pins the base dep *set*; this
    checks the platform marker specifically, since an unmarked `tzdata` would
    also satisfy a set comparison while pulling a useless wheel onto every
    Linux and macOS install.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert "tzdata" in text, (
        "`tzdata` is gone from pyproject.toml — Windows has no IANA tz "
        "database, so zoneinfo cannot resolve a named zone and coord/config.py"
        " + coord/models.py + coord/failure_class.py + coord/machine_pause.py "
        "all raise ZoneInfoNotFoundError on ordinary use (#2000)"
    )
    tzdata_lines = [ln for ln in text.splitlines() if "tzdata" in ln]
    assert any("sys_platform" in ln and "win32" in ln for ln in tzdata_lines), (
        "`tzdata` is declared without a `sys_platform == 'win32'` marker — it "
        f"is only needed on Windows. Lines found: {tzdata_lines}"
    )
