"""#3796: every pytest invocation in `.github/workflows/test.yml` is wall-time
bounded, and `pytest-timeout` is actually installed to back that up.

THE GAP THIS CLOSES. The known hang
(``tests/test_cli_assign.py::TestAssignDryRun::test_dry_run_no_network_dispatch``,
quarantined behind ``COORD_RUN_QUARANTINED_TESTS``) once ran unbounded inside a
CI job — reproduced locally, it ran 80+ minutes before anyone noticed. Adding
``--timeout=120 --timeout-method=signal`` to each `pytest`/`python -m pytest`
invocation fixes that, but only for as long as every such invocation keeps
carrying both flags; nothing stopped a future edit from dropping one while
touching an unrelated line nearby. This file is the regression guard, mirroring
``tests/test_windows_job_is_a_real_gate_2000.py``'s approach of parsing the
workflow YAML rather than grepping its text.

WHY `windows` IS A DELIBERATE EXCEPTION to `--timeout-method=signal`.
`signal.SIGALRM` does not exist on Windows, and pytest-timeout does not fall
back to the `thread` method silently when `signal` is explicitly requested on a
platform that lacks it — it raises ``AttributeError`` out of every test's setup
instead. The `windows` job's own `pytest` step is deliberately left with only
`--timeout=120`, letting pytest-timeout pick its own platform default
(`thread`). Every other job here runs on `ubuntu-latest`, where `signal` is
available and required (the hang is CPU-bound in the main thread, which the
`thread` method cannot interrupt).
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test.yml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

#: Jobs whose `pytest`/`python -m pytest` step(s) are exempt from
#: `--timeout-method=signal` because they run on a platform without
#: `signal.SIGALRM`. Keep this in lock-step with any future non-Linux job.
_NO_SIGALRM_JOBS = frozenset({"windows"})


def _load_workflow() -> dict:
    # YAML 1.1 parses a bare `on:` key as the boolean True under SafeLoader.
    # Harmless here (this test never reads it), but noted so a future reader
    # isn't surprised the key isn't the string "on".
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _non_echo_lines(run: str) -> str:
    """`run:` text with any `echo "..."` diagnostic lines stripped out.

    `populated-home`'s "What a red result here means" step (`if: failure()`)
    echoes a `python -m pytest ...` *repro command* as human-readable text —
    that mention is not an invocation pytest-timeout ever sees, so a naive
    substring search over the raw `run:` text misidentifies it as one and
    then fails looking for `--timeout=` inside a string that was never meant
    to carry it.
    """
    return "\n".join(
        line for line in run.splitlines() if not line.strip().startswith("echo")
    )


def _pytest_run_strings_by_job() -> dict[str, list[str]]:
    """Every `run:` step that actually invokes pytest, keyed by job name.

    The returned strings have any `echo` diagnostic lines already stripped
    (see `_non_echo_lines`), so callers can search them directly for
    `--timeout=` / `--timeout-method=` without re-filtering.

    A job may invoke pytest directly (``pytest ...``), through
    ``python -m pytest ...``, or wrapped by a harness script (e.g.
    ``scripts/run_tests_without_gh.sh python -m pytest ...``) — all three
    shapes contain the substring ``pytest`` in the invocation line, which is
    all this needs to find them.
    """
    jobs = _load_workflow().get("jobs") or {}
    found: dict[str, list[str]] = {}
    for job_name, job in jobs.items():
        runs = [
            filtered
            for step in (job.get("steps") or [])
            if isinstance(step.get("run"), str)
            and "pytest" in (filtered := _non_echo_lines(step["run"]))
        ]
        if runs:
            found[job_name] = runs
    return found


def test_every_pytest_invocation_has_a_wall_time_bound() -> None:
    by_job = _pytest_run_strings_by_job()
    assert by_job, (
        f"no job in {WORKFLOW_PATH} invokes pytest at all — expected at least "
        "the 'test' job to run the suite"
    )
    for job_name, runs in by_job.items():
        for run in runs:
            assert "--timeout=" in run, (
                f"job '{job_name}' invokes pytest without a `--timeout=` bound "
                f"(#3796 exists precisely because an unbounded run can hang CI "
                f"for 80+ minutes on a single test): {run!r}"
            )


def test_every_pytest_invocation_uses_the_signal_method_except_where_sigalrm_is_unavailable() -> (
    None
):
    by_job = _pytest_run_strings_by_job()
    for job_name, runs in by_job.items():
        for run in runs:
            if job_name in _NO_SIGALRM_JOBS:
                assert "--timeout-method=signal" not in run, (
                    f"job '{job_name}' is listed in _NO_SIGALRM_JOBS (no "
                    "signal.SIGALRM on its runner) but its pytest step "
                    f"requests `--timeout-method=signal` anyway, which "
                    f"pytest-timeout turns into an AttributeError on every "
                    f"test's setup rather than falling back silently: {run!r}"
                )
            else:
                assert "--timeout-method=signal" in run, (
                    f"job '{job_name}' invokes pytest without "
                    "`--timeout-method=signal` — the known hang "
                    "(test_dry_run_no_network_dispatch) is CPU-bound in the "
                    "main thread, and the default `thread` method cannot "
                    f"interrupt that (#3796): {run!r}"
                )


def test_pytest_timeout_is_a_dev_dependency() -> None:
    text = PYPROJECT_PATH.read_text()
    assert "pytest-timeout" in text, (
        "`pytest-timeout` is gone from pyproject.toml — the `--timeout=` / "
        "`--timeout-method=` flags asserted above in .github/workflows/test.yml "
        "are plugin options that `pip install -e \".[dev]\"` must actually "
        "provide, or every job above fails at argument-parsing before a "
        "single test runs (#3796)"
    )
