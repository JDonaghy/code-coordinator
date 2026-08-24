"""Regression tests for scripts/coord-test-runner.sh's baseline comparison (#2170).

THE STRUCTURAL GAP THIS CLOSES. The runner answered "did the suite pass?". The
Test gate is asked "did this branch make anything WORSE than `$BASE_REF` on this
machine?". Those differ in exactly one situation — the machine's own baseline is
red — and nothing detected it. Six tests failed on `origin/main` on any machine
with a populated `$HOME` and no `sqlite3` (all invisible to CI, whose `$HOME` is
empty), so the Test stage on `precision` could not produce a green verdict for
this repo on ANY branch: every dispatch returned `SMOKE: fail`, blamed the
branch, and cost a human verdict to adjudicate. The Test agent on
claude-coordinator#2158 said it outright — *"the runner has no known-good
baseline to compare against, so it reports pre-existing breakage as a branch
failure"*.

WHAT IS ASSERTED HERE, AND WHY IT IS MOSTLY THE NEGATIVE CASES. The new outcome
DOWNGRADES a verdict: it turns a red suite into "not the branch's fault". That is
the dangerous direction — the direction `coord.revalidate.is_infrastructure_failure`'s
docstring spends a paragraph refusing to guess in — because a wrong downgrade
eventually launders a real regression into a merge. So one test covers the
positive case and five cover the ways it must REFUSE to downgrade: a test that
passes on the base, a partial overlap, a branch-new test file, an inconclusive
baseline run, and a mechanically-unsound comparison. A flake still outranks
everything, and the baseline is never even consulted for one.

HOW THESE RUN IN MILLISECONDS. The runner reuses `$WT/.venv/bin/python` when one
already exists, so these tests plant a *fake* interpreter there — a bash script
that answers the runner's three distinct invocations (`-c "import xdist"`, the
`import coord` resolution probe, and `-m pytest ...`) from environment variables,
and tells the branch run apart from the baseline run by its own `$PWD`. No venv
is built, no pip runs, no real pytest is collected: what is under test is the
runner's DECISION LOGIC, and a real suite would only make that logic harder to
steer into each of its six branches.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# #2684: the module under test IS a bash script (scripts/coord-test-runner.sh),
# invoked here via `bash`, which in turn probes/execs `$WT/.venv/bin/python`
# and `$WT/.venv/bin/coord` — themselves `#!/usr/bin/env bash` fakes planted
# by the `repo` fixture and run directly off PATH, with no `bash` prefix, so
# their shebang is what makes them executable at all. That is category (1)
# from #2684 twice over (the runner itself, plus every fake it shells out
# to), and coord-test-runner.sh is a POSIX Test-stage/CI tool with no
# Windows port planned — "do not add a bash dependency to the Windows job"
# rules out fixing this by requiring bash there. No Windows port yet.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="exercises scripts/coord-test-runner.sh (a bash script) via "
    "bash-script fakes executed directly off PATH — POSIX-only (#2684)",
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "coord-test-runner.sh"

#: The two node ids the fake interpreter reports as failing on the branch.
#: Both files exist at the base commit (see the `repo` fixture), so they are
#: eligible to be baseline-red — the branch-new case gets its own test.
BRANCH_FAILED = "tests/test_ambient.py::test_one tests/test_ambient.py::test_two"

#: A bash stand-in for `python`. Placed at `$WT/.venv/bin/python`, which is
#: exactly the path the runner probes before deciding to build a venv, so
#: planting it here is what makes these tests free.
#:
#: It must answer three shapes of invocation, and must distinguish the BRANCH
#: run from the BASELINE run — which it does by comparing `$PWD` against the
#: worktree it lives inside, because that is the only difference the runner
#: creates between them.
_FAKE_PYTHON = r"""#!/usr/bin/env bash
# Where this fake lives: <worktree>/.venv/bin/python -> <worktree>
_wt="$(cd "$(dirname "$0")/../.." && pwd -P)"
_here="$(pwd -P)"

_emit_failed() {
    local id
    for id in $1; do
        printf 'FAILED %s - AssertionError\n' "$id"
    done
}

if [[ "$1" == "-c" ]]; then
    case "$2" in
        *xdist*)
            # "is pytest-xdist importable?" — no, so the runner goes serial and
            # its command line stays predictable for the branches below.
            exit 1 ;;
        *coord*)
            # The `import coord` resolution probe: does this venv resolve the
            # package from the BASELINE worktree rather than the branch's?
            exit "${FAKE_COORD_PROBE_EXIT:-0}" ;;
        *)
            exit 0 ;;
    esac
fi

if [[ "$1" == "-m" && "$2" == "pytest" ]]; then
    # An invocation carrying node ids is a targeted re-run (the flake filter, or
    # the baseline comparison); one without is the full-suite run.
    _targeted=0
    for arg in "$@"; do
        case "$arg" in *::*) _targeted=1 ;; esac
    done

    if [[ "$_here" == "$_wt" ]]; then
        # ── the BRANCH's own worktree ────────────────────────────────────────
        if [[ "$_targeted" -eq 1 && "${FAKE_RERUN_PASSES:-0}" == "1" ]]; then
            printf '%s passed\n' "2"
            exit 0
        fi
        # #2562: a PARTIAL isolation re-run — one node id genuinely still
        # fails, the rest passed. Distinct from FAKE_RERUN_PASSES (all pass,
        # exit 0) and from the "no override" default (everything in
        # FAKE_BRANCH_FAILED fails again, as if isolation changed nothing).
        if [[ "$_targeted" -eq 1 && -n "${FAKE_RERUN_STILL_FAILING:-}" ]]; then
            _emit_failed "$FAKE_RERUN_STILL_FAILING"
            printf '1 failed, 1 passed\n'
            exit 1
        fi
        _emit_failed "${FAKE_BRANCH_FAILED:-}"
        printf '2 failed\n'
        exit 1
    fi

    # ── the BASELINE worktree ────────────────────────────────────────────────
    if [[ "${FAKE_BASELINE_ERROR:-0}" == "1" ]]; then
        printf 'ERROR tests/test_ambient.py - ImportError: no module named nope\n'
        exit 2
    fi
    if [[ -z "${FAKE_BASELINE_FAILED:-}" ]]; then
        printf '2 passed\n'
        exit 0
    fi
    _emit_failed "${FAKE_BASELINE_FAILED:-}"
    printf 'failed\n'
    exit 1
fi

exit 0
"""

#: A bash stand-in for `coord`, placed at `$WT/.venv/bin/coord` — exactly
#: where `run_python_acceptance_ci` (#2180 review fix) looks for it after a
#: green (or flake-tolerated) pytest run, to run the sealed cli-pytest
#: acceptance route through `coord acceptance run --all --ci`. Without this
#: stub every test below that reaches a PASS/FLAKE verdict would fail with
#: "No such file or directory" the moment that call fires, since the fake
#: venv planted by the `repo` fixture is bash, not a real `pip install -e
#: .[dev]`. Records its argv (one line per invocation) to
#: `$FAKE_COORD_ARGV_LOG` when set, so tests can assert on the exact
#: `coord acceptance run` invocation shape, and exits `$FAKE_ACCEPTANCE_EXIT`
#: (default 0 — green) to simulate the sealed suite's `--ci` verdict.
_FAKE_COORD = r"""#!/usr/bin/env bash
if [[ -n "${FAKE_COORD_ARGV_LOG:-}" ]]; then
    printf '%s\n' "$*" >> "$FAKE_COORD_ARGV_LOG"
fi
if [[ -n "${FAKE_COORD_PATH_LOG:-}" ]]; then
    printf '%s\n' "$PATH" >> "$FAKE_COORD_PATH_LOG"
fi
if [[ -n "${FAKE_COORD_ENV_LOG:-}" ]]; then
    printf 'HOME=%s\nCOORD_SERVICE_URL=%s\n' \
        "${HOME:-}" "${COORD_SERVICE_URL:-<unset>}" >> "$FAKE_COORD_ENV_LOG"
fi
exit "${FAKE_ACCEPTANCE_EXIT:-0}"
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo shaped like a real Test leg: a `base` commit that already carries
    the test files, plus one branch commit touching `coord/**` so routing picks
    the python arm.

    The test files must exist at `base`, because "does this node id's file exist
    on the merge-base?" is one of the runner's refusal conditions — a branch-new
    test cannot have been failing already. `tests/test_new.py` is added only by
    the test that exercises that refusal.
    """
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    (r / "coord").mkdir(parents=True)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "Test")
    (r / "tests" / "test_ambient.py").write_text(
        "def test_one():\n    assert True\n", encoding="utf-8"
    )
    (r / "README.md").write_text("init\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "initial")
    _git(r, "tag", "base")

    # One branch commit under coord/** — routing sends this to the python arm.
    (r / "coord" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "branch change")

    venv_bin = r / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake = venv_bin / "python"
    fake.write_text(_FAKE_PYTHON, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    fake_coord = venv_bin / "coord"
    fake_coord.write_text(_FAKE_COORD, encoding="utf-8")
    fake_coord.chmod(fake_coord.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return r


def _run(repo: Path, **fake_env: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("FAKE_BRANCH_FAILED", BRANCH_FAILED)
    env.update(fake_env)
    return subprocess.run(
        ["bash", str(SCRIPT), str(repo), "--base-ref", "base", "--repo", "code-coordinator"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=env,
    )


# ── the positive case: the machine's baseline is red ────────────────────────


def test_baseline_red_when_every_failure_reproduces_on_the_merge_base(repo: Path) -> None:
    """#2170's headline: the exact `precision` situation, now named as such.

    Asserts on the OUTPUT as well as the exit code, because the output is what a
    human (and the Test agent writing the report) acts on — and the whole point
    is that the operator must not be sent to debug a branch that is fine. The
    `RESULT: FAIL` absence check is load-bearing: a run that says both is worse
    than one that says neither.
    """
    result = _run(repo, FAKE_BASELINE_FAILED=BRANCH_FAILED)

    assert result.returncode == 4, (result.returncode, result.stdout, result.stderr)
    assert "RESULT: BASELINE-RED (python)" in result.stdout
    assert "BASELINE-RED(python): all 2 failing test(s) fail identically" in result.stdout
    assert "RESULT: FAIL" not in result.stdout
    assert "FAIL(python)" not in result.stdout
    # The failing ids are still listed — "baseline is red" is only actionable if
    # it says WHICH tests, so someone can go and fix the baseline.
    assert "tests/test_ambient.py::test_one" in result.stdout
    # And it must say out loud that this is not a verdict on the branch.
    assert "NOT a verdict on the branch" in result.stdout


def test_baseline_red_exit_code_is_distinct_from_fail_and_infra(repo: Path) -> None:
    """4, not 1 and not 3. A consumer that has never heard of baseline-red sees a
    non-zero exit and treats it as a failure — the SAFE default, i.e. exactly
    today's behaviour — while one that has can route it as "no verdict". Reusing
    1 would make it undetectable; reusing 3 (INFRA) would claim the suite never
    ran, when in fact it ran and told us something useful.
    """
    baseline_red = _run(repo, FAKE_BASELINE_FAILED=BRANCH_FAILED)
    genuine = _run(repo)  # baseline green ⇒ the branch owns it

    assert baseline_red.returncode == 4
    assert genuine.returncode == 1
    assert baseline_red.returncode != genuine.returncode


def test_baseline_worktree_is_cleaned_up(repo: Path) -> None:
    """The scratch worktree must not survive the run.

    A leaked `git worktree` registration accumulates one entry per red Test leg
    on a long-lived checkout, and `git worktree list` is how an operator sees
    what is going on — so this is the kind of mess that gets noticed weeks later
    on the machine that can least afford confusion.
    """
    result = _run(repo, FAKE_BASELINE_FAILED=BRANCH_FAILED)
    assert result.returncode == 4

    listed = _git(repo, "worktree", "list")
    # Exactly one entry — the repo itself. Matching on the *count* rather than
    # on the absent path, because `tmp_path` is named after this test and so
    # already contains the substring "baseline".
    assert len(listed.splitlines()) == 1, listed
    assert listed.startswith(str(repo)), listed


# ── the refusals: everything that must stay a branch FAIL ───────────────────


def test_green_baseline_is_a_genuine_branch_failure(repo: Path) -> None:
    """The common case, unchanged: the tests pass on the base, so the branch
    broke them."""
    result = _run(repo)  # FAKE_BASELINE_FAILED unset ⇒ baseline is green

    assert result.returncode == 1
    assert "RESULT: FAIL (python)" in result.stdout
    assert "BASELINE-RED" not in result.stdout


def test_partial_overlap_is_a_branch_failure(repo: Path) -> None:
    """One of the two failures reproduces on the base, the other does not.

    The baseline run exits non-zero here, so an implementation that keyed on the
    base run's EXIT CODE rather than on its failing SET would call this
    baseline-red and hide a real regression. That is the single most likely way
    to get this feature wrong, so it gets its own test.
    """
    result = _run(repo, FAKE_BASELINE_FAILED="tests/test_ambient.py::test_one")

    assert result.returncode == 1
    assert "RESULT: FAIL (python)" in result.stdout
    assert "BASELINE-RED" not in result.stdout
    assert "test_two PASSES on the merge-base" in result.stdout


def test_branch_new_test_file_can_never_be_baseline_red(repo: Path) -> None:
    """A test the branch ADDED cannot have been failing already.

    Without this check the baseline run would be handed a node id whose file does
    not exist, pytest would exit non-zero for a usage/collection reason, and a
    brand-new broken test would be excused as "pre-existing" — the worst possible
    version of this bug.
    """
    (repo / "tests" / "test_new.py").write_text(
        "def test_new():\n    assert False\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add a new test")

    result = _run(
        repo,
        FAKE_BRANCH_FAILED="tests/test_new.py::test_new",
        FAKE_BASELINE_FAILED="tests/test_new.py::test_new",
    )

    assert result.returncode == 1
    assert "BASELINE-RED" not in result.stdout
    assert "does not exist on the merge-base" in result.stdout
    assert "branch-new" in result.stdout


def test_inconclusive_baseline_run_is_not_baseline_red(repo: Path) -> None:
    """A collection/import error on the base means the base suite never ran, so
    it says nothing about these tests — the same "not a verdict" reasoning as
    #1814's INFRA class, applied to the comparison instead of the run."""
    result = _run(repo, FAKE_BASELINE_ERROR="1")

    assert result.returncode == 1
    assert "RESULT: FAIL (python)" in result.stdout
    assert "BASELINE-RED" not in result.stdout
    assert "says nothing about these tests" in result.stderr + result.stdout


def test_comparison_is_skipped_when_the_venv_resolves_coord_wrongly(repo: Path) -> None:
    """The baseline run reuses the BRANCH's venv (building a second one would
    double a red Test leg's cost — #2169). That is only sound if `import coord`
    resolves from the baseline worktree, which depends on how pip wrote the
    editable install. The runner asserts it instead of assuming it; when the
    assertion fails it must skip the comparison, not answer it wrongly.
    """
    result = _run(
        repo,
        FAKE_BASELINE_FAILED=BRANCH_FAILED,  # would otherwise be baseline-red
        FAKE_COORD_PROBE_EXIT="9",
    )

    assert result.returncode == 1
    assert "BASELINE-RED" not in result.stdout
    assert "does not resolve 'coord' from the baseline worktree" in (
        result.stderr + result.stdout
    )


def test_missing_base_ref_reports_fail_uncompared(repo: Path) -> None:
    """No resolvable merge-base ⇒ no comparison, and the verdict stays what it
    was. Fail-soft in the direction that cannot launder anything."""
    result = subprocess.run(
        [
            "bash", str(SCRIPT), str(repo),
            "--base-ref", "refs/heads/no-such-ref",
            "--repo", "code-coordinator",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env={**os.environ, "FAKE_BRANCH_FAILED": BRANCH_FAILED,
             "FAKE_BASELINE_FAILED": BRANCH_FAILED},
    )

    assert result.returncode == 1
    assert "BASELINE-RED" not in result.stdout
    assert "uncompared" in result.stderr + result.stdout


# ── precedence: a flake still outranks the comparison ───────────────────────


def test_a_flake_is_still_a_flake_and_never_consults_the_baseline(repo: Path) -> None:
    """Flake filtering runs FIRST and short-circuits.

    If the failures pass in isolation the run is a tolerated PASS, and the
    baseline worktree must never even be created — otherwise every flaky Test leg
    would pay for a checkout it does not need. `[test] baseline:` appearing in
    the log is the observable proof it was not consulted.
    """
    result = _run(
        repo,
        FAKE_RERUN_PASSES="1",
        FAKE_BASELINE_FAILED=BRANCH_FAILED,
    )

    assert result.returncode == 0
    assert "FLAKE(python)" in result.stdout
    assert "RESULT: PASS" in result.stdout
    assert "baseline:" not in result.stdout + result.stderr


# ── #2562: partial-flake pruning feeds baseline comparison ──────────────────
#
# The all-or-nothing bug: `pytest $failed` re-ran the whole failing set
# together, so ONE genuine failure alongside a flake kept the flake in
# `$failed` — and that poisoned set then went to `python_baseline_is_red`,
# itself unanimity-gated, where the flake (which passes on the baseline same
# as everywhere else) short-circuited a correct BASELINE-RED downgrade. The
# fix rebuilds `$failed` from the isolation re-run's own `FAILED ` lines
# before doing anything else with it.


def test_mixed_flake_and_genuine_prunes_to_baseline_red(repo: Path) -> None:
    """{genuine, flake} prunes to {genuine}, and BASELINE-RED fires on just
    that remainder — the exact CC#2556 shape from the issue: a real flake
    sitting next to a test that fails identically on the merge-base must not
    suppress the downgrade.
    """
    result = _run(
        repo,
        # Isolation re-run: test_one still fails, test_two (the flake) passed
        # and dropped out.
        FAKE_RERUN_STILL_FAILING="tests/test_ambient.py::test_one",
        # Baseline reproduces the one that's left.
        FAKE_BASELINE_FAILED="tests/test_ambient.py::test_one",
    )

    assert result.returncode == 4, (result.returncode, result.stdout, result.stderr)
    assert "RESULT: BASELINE-RED (python)" in result.stdout
    assert "1 of 2 failing test(s) passed in isolation" in result.stdout
    assert "comparing the remaining 1 against the baseline" in result.stdout
    assert "BASELINE-RED(python): all 1 failing test(s) fail identically" in result.stdout
    # The pruned flake must not appear as part of the (now genuine-only) set.
    assert "tests/test_ambient.py::test_one" in result.stdout
    assert "RESULT: FAIL" not in result.stdout


def test_mixed_flake_and_genuine_prunes_to_a_smaller_genuine_fail(repo: Path) -> None:
    """Same pruning, but the remainder is NOT baseline-red: still a FAIL, but
    for the pruned count and node id only — the reported list must match
    what actually failed on the isolation re-run, not the original set.
    """
    result = _run(
        repo,
        FAKE_RERUN_STILL_FAILING="tests/test_ambient.py::test_one",
        # FAKE_BASELINE_FAILED unset ⇒ baseline is green for test_one too.
    )

    assert result.returncode == 1
    assert "RESULT: FAIL (python)" in result.stdout
    assert "BASELINE-RED" not in result.stdout
    assert "1 of 2 failing test(s) passed in isolation" in result.stdout
    assert "FAIL(python): 1 test(s) fail on re-run — genuine" in result.stdout
    assert "tests/test_ambient.py::test_one" in result.stdout
    assert "tests/test_ambient.py::test_two" not in (
        result.stdout.split("FAIL(python): 1 test(s) fail on re-run — genuine")[1]
    )


def test_all_genuine_failing_set_is_unaffected_by_pruning(repo: Path) -> None:
    """Both failures still fail in isolation ⇒ nothing pruned, and behaviour
    matches the pre-#2562 baseline-red path exactly (no pruning log line)."""
    result = _run(repo, FAKE_BASELINE_FAILED=BRANCH_FAILED)

    assert result.returncode == 4
    assert "RESULT: BASELINE-RED (python)" in result.stdout
    assert "passed in isolation" not in result.stdout


# ── #2180 review fix: the Test stage must agree with CI on tests/acceptance ─
#
# Before this fix, `--ignore=tests/acceptance` dropped the sealed cli-pytest
# route (ms-37) from the Test stage entirely, with nothing replacing it —
# so a test-id listed in some ms-NN/manifest.yml's `expected_red:` that
# unexpectedly PASSED (the #1965 vacuous-assertion case) would be silently
# waved through here while CI's `test` job (which does run the #2164 `--ci`
# wrapper, no continue-on-error) correctly reddened for the identical
# branch. These two tests pin that the Test stage now runs the SAME wrapper
# CI does, and that its result actually gates the python arm.


def test_a_passing_python_suite_also_runs_the_acceptance_ci_wrapper(repo: Path, tmp_path: Path) -> None:
    """Any path through `run_python` that reaches a PASS/FLAKE verdict must
    invoke `coord acceptance run --all --ci` for this repo's cli-pytest
    route (ms-37) — not just the ordinary `--ignore=tests/acceptance` suite
    — with the same `--repo`/`--all`/`--ci` contract CI's own `test` job
    step uses (tests/test_ci_acceptance_gate_1950.py pins that contract for
    the workflow file; this pins it for the Test stage's engine).

    Uses the same `FAKE_RERUN_PASSES` flake path as the precedence test
    above to reach a PASS — the fake `python` stub's full (non-targeted)
    run always reports failures first, same as a real flaky suite; only
    the isolated re-run can report green."""
    argv_log = tmp_path / "coord-argv.log"
    path_log = tmp_path / "coord-path.log"
    env_log = tmp_path / "coord-env.log"
    result = _run(
        repo,
        FAKE_RERUN_PASSES="1",
        FAKE_COORD_ARGV_LOG=str(argv_log),
        FAKE_COORD_PATH_LOG=str(path_log),
        FAKE_COORD_ENV_LOG=str(env_log),
        # Simulate a board_service-configured host (thin client, or a daemon
        # host whose client.toml points at its own board service): the
        # runner must strip this before invoking `coord`, or _load_config's
        # #1080 thin-client branch fetches the fleet's remote config and
        # silently discards the --config we assert on above.
        COORD_SERVICE_URL="http://board.example:7435",
    )

    assert result.returncode == 0
    assert "RESULT: PASS" in result.stdout

    assert argv_log.exists(), (
        "the python arm never invoked the fake `coord` at all — "
        "run_python_acceptance_ci did not run"
    )
    invocation = argv_log.read_text(encoding="utf-8").strip()
    assert "acceptance run" in invocation
    assert "--repo claude-coordinator" in invocation
    assert "--all" in invocation
    assert "--ci" in invocation
    # The in-repo CI fragment, NOT the daemon host's default-resolved
    # ~/.coord/coordinator.yml: the fleet's cli-pytest route is
    # `--issue`-scoped (`pytest tests/acceptance/{ms}`), and `--all` leaves
    # `{ms}` literal — pytest collects 0 tests and this arm goes red for
    # every branch touching coord/** (issue-2235's Test leg, 2026-08-15).
    assert "--config" in invocation, (
        "run_python_acceptance_ci passed no --config — `coord acceptance "
        "run --all` would resolve the fleet's ~/.coord/coordinator.yml, "
        "whose {ms}-templated cli-pytest route collects 0 tests under --all"
    )
    assert ".github/coord-ci-acceptance.yml" in invocation, (
        f"--config does not point at the in-repo CI fragment: {invocation!r}"
    )
    # The fragment's route is a bare `pytest tests/acceptance`, resolved
    # from PATH by the driver's shell — the branch venv's bin/ must be
    # prepended, or the daemon's systemd-user PATH supplies a pytest with
    # none of this repo's deps (or none at all).
    recorded_path = path_log.read_text(encoding="utf-8").strip()
    assert recorded_path.startswith(str(repo / ".venv" / "bin") + os.pathsep), (
        "run_python_acceptance_ci did not prepend the venv's bin/ to PATH — "
        "the cli-pytest route's bare `pytest` would resolve outside the "
        f"branch venv: PATH={recorded_path!r}"
    )
    # --config is necessary but NOT sufficient: `_load_config`'s #1080
    # thin-client branch (coord/commands/_common.py) checks "is a board
    # service configured" BEFORE honouring the explicit path, so on any
    # host with $COORD_SERVICE_URL or a real ~/.coord/client.toml the flag
    # is silently discarded in favour of the fleet's remote
    # {ms}-templated config — the exact `tests/acceptance/{ms}` 0-collected
    # failure the --config-only version of this fix still produced
    # (issue-2235's second Test leg, 2026-08-15). The runner must invoke
    # `coord` like a CI runner: scratch $HOME (no client.toml) and the
    # service env vars stripped.
    recorded_env = env_log.read_text(encoding="utf-8")
    assert "COORD_SERVICE_URL=<unset>" in recorded_env, (
        "run_python_acceptance_ci leaked COORD_SERVICE_URL through to "
        "`coord` — _load_config's thin-client branch will fetch the remote "
        f"fleet config and discard --config: {recorded_env!r}"
    )
    env_home = next(
        (line.removeprefix("HOME=") for line in recorded_env.splitlines()
         if line.startswith("HOME=")),
        "",
    )
    assert env_home == str(repo / ".coord-ci-home"), (
        "run_python_acceptance_ci did not point $HOME at the scratch "
        "in-worktree home — a real ~/.coord/client.toml would re-arm the "
        f"thin-client branch even with the env vars stripped: {recorded_env!r}"
    )


def test_a_red_acceptance_ci_wrapper_fails_the_python_arm(repo: Path) -> None:
    """The whole point of wiring the wrapper in: when `coord acceptance run
    --all --ci` reports non-green (a test NOT in `expected_red` failed, or
    one listed in it unexpectedly passed), that must fail the Test stage —
    exactly as it fails CI's `test` job, which carries no
    continue-on-error on this step. Excluding tests/acceptance with no
    replacement call (the pre-fix state) could never produce this result no
    matter what the sealed suite did.

    `FAKE_RERUN_PASSES=1` gets the ordinary suite to green first, so the
    FAIL asserted below is attributable to the acceptance wrapper alone,
    not to the ordinary suite's own (unrelated) failure path."""
    result = _run(repo, FAKE_RERUN_PASSES="1", FAKE_ACCEPTANCE_EXIT="1")

    assert result.returncode == 1
    assert "RESULT: FAIL (python)" in result.stdout
    assert "FAIL(python)" in result.stdout
    assert "acceptance" in result.stdout.lower()
