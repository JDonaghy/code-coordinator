"""Regression tests for the populated-``$HOME`` arm of scripts/coord-test-runner.sh (#2269).

THE STRUCTURAL GAP THIS CLOSES. A branch could be green at the Test gate and red
in CI, and **whether that was caught depended on which machine the Test stage
landed on**. CI's ``populated-home`` job does not use the runner's ``$HOME`` — it
*synthesizes* a hostile one via ``scripts/run_tests_in_populated_home.sh`` (a
thin-client ``~/.coord`` with no ``coordinator.yml``, ``sqlite3`` masked off
``$PATH``, and a ``$TMPDIR`` whose ancestor carries a ``[tool.pytest.ini_options]``).
The Test stage, by contrast, ran in whatever environment the assigned machine
happened to have. dellserver is the *daemon host*: it has a real
``coordinator.yml`` and is not a thin client, so knob 1 is structurally absent
there. #2174's leg ran on dellserver, recorded ``test_state=passed``, spent a
full adversarial review — and CI was red on ``populated-home`` the whole time,
with four assertion failures ``coord fix`` eventually had to adjudicate against
the stored verdict (#2091's conflict message).

WHY THE EXISTING GUARDS DID NOT COVER IT. ``tests/test_ambient_home_isolation.py``
runs everywhere, but by design it drives *the three known ambient-sensitive
targets* — it cannot catch a **newly written** test of this class, which is
exactly what #2174 was. And #2170's baseline check is the *inverse* mechanism: it
downgrades a FAIL when the machine's baseline is already red. Nothing made a
green machine reproduce a red one.

WHAT IS ASSERTED HERE, AND WHY IT LEANS ON THE FAILURE CASES. This arm is the
STRICT direction — it can only ever turn a green run red, never the reverse — so
the tests that matter most are the ones pinning that it cannot be silently
skipped: the harness's own ``exit 2`` ("a knob failed to take effect") must be a
FAIL-with-warning, an unrecognised non-zero exit must still be a FAIL, and every
legitimate skip must be *announced* both in the verdict stream and by
``--print-routing``.

HOW THESE RUN IN MILLISECONDS. Same technique as
``tests/test_coord_test_runner_baseline.py``: a *fake* interpreter is planted at
``$WT/.venv/bin/python`` (the exact path the runner probes before building a
venv), so no venv is built and no real pytest is collected. The fake tells the
runner's four distinct pytest invocations apart by their argument shape — the
full run carries ``--ignore=tests/acceptance``, the flake/baseline re-runs carry
``::`` node ids, and what is left is the populated-``$HOME`` arm. The
``run_tests_in_populated_home.sh`` copied into the fixture repo is the REAL one,
so the knob assertions below are about the environment the arm actually creates.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from .conftest import POSIX_BASH

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "coord-test-runner.sh"
HARNESS = REPO_ROOT / "scripts" / "run_tests_in_populated_home.sh"

#: Where the harness lives relative to a worktree. The runner invokes the copy in
#: the BRANCH's tree, not this checkout's, so a branch that edits the harness is
#: tested against its own version — exactly like CI.
HARNESS_REL = "scripts/run_tests_in_populated_home.sh"

#: A bash stand-in for `python`, planted at `$WT/.venv/bin/python`.
#:
#: It must answer four shapes of invocation and keep them apart, because the arm
#: under test is defined by being the one that is NEITHER the full suite NOR a
#: targeted re-run:
#:
#:   `-c ...`                                 → the xdist / `import coord` probes
#:   `-m pytest ... --ignore=tests/acceptance` → the ordinary full suite
#:   `-m pytest ... <nodeid>::<test>`          → flake filter or #2170 baseline
#:   `-m pytest ... tests/foo.py`              → THE POPULATED-$HOME ARM
#:
#: Keying on argument shape rather than on `$HOME` is deliberate: on a thin
#: client (`precision`) the machine's REAL `$HOME` has the same shape the harness
#: synthesizes, so a `$HOME`-based check would misfire there and make this test
#: file itself machine-dependent — the very bug being fixed.
_FAKE_PYTHON = r"""#!/usr/bin/env bash
_wt="$(cd "$(dirname "$0")/../.." && pwd -P)"
_here="$(pwd -P)"

if [[ "$1" == "-c" ]]; then
    case "$2" in
        *xdist*) exit 1 ;;   # no xdist → serial, so the command line stays predictable
        *coord*) exit 0 ;;   # the baseline run's `import coord` resolution probe
        *)       exit 0 ;;
    esac
fi

if [[ "$1" == "-m" && "$2" == "pytest" ]]; then
    _targeted=0
    _full=0
    for arg in "$@"; do
        case "$arg" in
            *::*)                      _targeted=1 ;;
            --ignore=tests/acceptance) _full=1 ;;
        esac
    done

    if [[ "$_full" -eq 0 && "$_targeted" -eq 0 ]]; then
        # ── the populated-$HOME arm ──────────────────────────────────────────
        if [[ -n "${FAKE_POPULATED_ARGV_LOG:-}" ]]; then
            printf '%s\n' "$*" >>"$FAKE_POPULATED_ARGV_LOG"
        fi
        if [[ -n "${FAKE_POPULATED_ENV_LOG:-}" ]]; then
            {
                printf 'CWD=%s\n' "$_here"
                printf 'HOME=%s\n' "${HOME:-<unset>}"
                printf 'TMPDIR=%s\n' "${TMPDIR:-<unset>}"
                if [[ -f "$HOME/.coord/client.toml" ]]; then
                    printf 'client_toml=yes\n'
                else
                    printf 'client_toml=no\n'
                fi
                if [[ -e "$HOME/.coord/coordinator.yml" ]]; then
                    printf 'coordinator_yml=yes\n'
                else
                    printf 'coordinator_yml=no\n'
                fi
                if [[ -f "$HOME/pyproject.toml" ]]; then
                    printf 'ancestor_pytest_config=yes\n'
                else
                    printf 'ancestor_pytest_config=no\n'
                fi
                if command -v sqlite3 >/dev/null 2>&1; then
                    printf 'sqlite3=resolvable\n'
                else
                    printf 'sqlite3=masked\n'
                fi
            } >>"$FAKE_POPULATED_ENV_LOG"
        fi
        _rc="${FAKE_POPULATED_EXIT:-0}"
        if [[ "$_rc" != "0" ]]; then
            printf 'FAILED tests/test_ambient.py::test_one - AssertionError\n'
            printf '1 failed\n'
        fi
        exit "$_rc"
    fi

    if [[ "$_full" -eq 1 ]]; then
        # ── the ordinary full suite ──────────────────────────────────────────
        if [[ "${FAKE_SUITE_EXIT:-0}" != "0" ]]; then
            printf 'FAILED tests/test_ambient.py::test_one - AssertionError\n'
            printf '1 failed\n'
            exit "${FAKE_SUITE_EXIT}"
        fi
        printf '1 passed\n'
        exit 0
    fi

    # ── a targeted re-run: the flake filter (branch cwd) or #2170's baseline ──
    if [[ "$_here" == "$_wt" ]]; then
        printf 'FAILED tests/test_ambient.py::test_one - AssertionError\n'
        printf '1 failed\n'
        exit 1
    fi
    # On the merge-base everything passes, so a red ordinary suite stays a plain
    # branch FAIL rather than becoming #2170's BASELINE-RED (exit 4).
    printf '1 passed\n'
    exit 0
fi

exit 0
"""

#: A bash stand-in for `coord`, at `$WT/.venv/bin/coord` — where
#: `run_python_acceptance_ci` (#2180) looks for it once the ordinary suite is
#: green. Without it every green path here would die on "No such file or
#: directory" before the populated-$HOME arm was ever reached.
_FAKE_COORD = r"""#!/usr/bin/env bash
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


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo shaped like a real Test leg whose diff TOUCHES A PYTHON TEST FILE.

    That is the #2174 shape: a branch that writes or edits a test, which the
    always-on ``tests/test_ambient_home_isolation.py`` pin cannot cover because
    the test did not exist when that pin was written.

    The real ``run_tests_in_populated_home.sh`` is copied in at the base commit,
    so the arm exercises the genuine harness and the diff stays scoped to the one
    test file.
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
    (r / "scripts").mkdir(parents=True)
    shutil.copy2(HARNESS, r / HARNESS_REL)
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "initial")
    _git(r, "tag", "base")

    # The branch commit: it edits a python test file, so the arm is in scope.
    (r / "tests" / "test_ambient.py").write_text(
        "def test_one():\n    assert True\n\n\ndef test_two():\n    assert True\n",
        encoding="utf-8",
    )
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "branch change")

    _executable(r / ".venv" / "bin" / "python", _FAKE_PYTHON)
    _executable(r / ".venv" / "bin" / "coord", _FAKE_COORD)
    return r


def _run(repo: Path, *extra_args: str, **fake_env: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(fake_env)
    return subprocess.run(
        [
            POSIX_BASH, str(SCRIPT), str(repo),
            "--base-ref", "base",
            "--repo", "code-coordinator",
            *extra_args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        env=env,
    )


# ── --print-routing must name the arm before a worker pushes ────────────────


def test_print_routing_names_the_arm_when_the_diff_touches_a_test_file(repo: Path) -> None:
    """#2269's "a worker can see it before pushing" requirement.

    ``--print-routing`` short-circuits before anything is built or run, so this
    is the cheap way to ask "will my branch pay for this arm, and over what?".
    """
    result = _run(repo, "--print-routing")

    assert result.returncode == 0
    assert result.stdout.strip().splitlines()[-1] == (
        "ROUTING mode=coordinator pytest=1 populated-home=1"
    )
    assert "populated-$HOME arm (#2269) will re-run 1 diff-scoped test file(s)" in result.stdout
    assert "tests/test_ambient.py" in result.stdout


def test_print_routing_says_the_arm_is_skipped_for_a_diff_with_no_test_files(
    repo: Path,
) -> None:
    """A branch touching only ``coord/**`` still runs pytest, but has nothing for
    this arm to re-run — and that must be *stated*, not inferred from silence."""
    (repo / "coord" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--amend", "-qm", "coord-only change")
    # Undo the test-file edit so the diff really is coord/**-only.
    _git(repo, "checkout", "base", "--", "tests/test_ambient.py")
    _git(repo, "commit", "-aqm", "revert the test edit")

    result = _run(repo, "--print-routing")

    assert result.returncode == 0
    assert result.stdout.strip().splitlines()[-1] == (
        "ROUTING mode=coordinator pytest=1 populated-home=0"
    )
    assert "populated-$HOME arm (#2269) SKIPPED" in result.stdout
    assert "no python test files" in result.stdout


def test_the_sealed_acceptance_suite_is_not_in_the_arms_scope(repo: Path) -> None:
    """``tests/acceptance/**`` is excluded for the same reason the ordinary arm
    passes ``--ignore=tests/acceptance``: that sealed suite is gated separately
    through the #2164 ``--ci`` wrapper, and a red-by-design slice must not fail
    every concurrent branch (the quadraui#554/#490 failure mode #2180 closes)."""
    acc = repo / "tests" / "acceptance" / "ms-99"
    acc.mkdir(parents=True)
    (acc / "test_sealed.py").write_text(
        "def test_sealed():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "checkout", "base", "--", "tests/test_ambient.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "a sealed acceptance test only")

    result = _run(repo, "--print-routing")

    assert result.stdout.strip().splitlines()[-1] == (
        "ROUTING mode=coordinator pytest=1 populated-home=0"
    )


# ── the green path: scoped to the diff, and actually populated ──────────────


def test_a_green_branch_runs_the_arm_over_only_the_diff_scoped_test_files(
    repo: Path, tmp_path: Path
) -> None:
    """The cost constraint (#2169) made concrete: the arm re-runs the diff's test
    files, NOT the suite. The full suite already exceeds the 600s Bash ceiling
    once; running it twice is not on the table, and diff-scoping is the whole
    reason this arm is affordable."""
    argv_log = tmp_path / "populated-argv.log"
    result = _run(repo, FAKE_POPULATED_ARGV_LOG=str(argv_log))

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "RESULT: PASS" in result.stdout
    assert "PASS(python-populated-home): 1 diff-scoped test file(s) green" in result.stdout

    assert argv_log.exists(), (
        "the populated-$HOME arm never ran — a green python arm must always "
        "reach it (or say why it was skipped)"
    )
    invocation = argv_log.read_text(encoding="utf-8").strip()
    assert invocation.endswith("tests/test_ambient.py"), invocation
    # Scoped, not the suite: no bare `tests/` directory argument, and none of
    # the full run's markers.
    assert "--ignore=tests/acceptance" not in invocation
    assert " tests/ " not in f" {invocation} "


def test_the_arm_really_runs_under_the_three_knobs(repo: Path, tmp_path: Path) -> None:
    """The arm is worthless if it re-runs the tests in the SAME environment.

    Asserts the three knobs ``scripts/run_tests_in_populated_home.sh``'s header
    names, from inside the re-run itself: a thin-client ``~/.coord`` with no
    ``coordinator.yml`` (the knob dellserver is structurally incapable of
    reproducing, and the one that hid #2174), ``sqlite3`` off ``$PATH``, and a
    ``$TMPDIR`` whose ancestor carries a pytest config.
    """
    env_log = tmp_path / "populated-env.log"
    result = _run(repo, FAKE_POPULATED_ENV_LOG=str(env_log))

    assert result.returncode == 0, (result.stdout, result.stderr)
    recorded = dict(
        line.split("=", 1)
        for line in env_log.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert recorded["client_toml"] == "yes", recorded
    assert recorded["coordinator_yml"] == "no", recorded
    assert recorded["ancestor_pytest_config"] == "yes", recorded
    assert recorded["sqlite3"] == "masked", recorded
    assert recorded["TMPDIR"] == f"{recorded['HOME']}/tmp", recorded
    # $HOME is synthesized, never the machine's own.
    assert recorded["HOME"] != os.environ.get("HOME"), recorded
    # ...and the tests still run from the branch worktree, so pytest's rootdir
    # is the repo's and not the synthesized home's.
    assert recorded["CWD"] == str(Path(repo).resolve()), recorded


# ── the failure this whole issue exists for ─────────────────────────────────


def test_a_failure_only_in_a_populated_home_fails_the_test_stage(repo: Path) -> None:
    """#2174, reproduced: the ordinary suite is green on this machine and the
    same files are red in a fleet-shaped ``$HOME``.

    Before #2269 this was ``RESULT: PASS`` on dellserver and red in CI. The
    verdict line must NAME the arm — "a red result is attributable, not a
    mystery", the same standard the sealed-acceptance line already meets.
    """
    result = _run(repo, FAKE_POPULATED_EXIT="1")

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "FAIL(python-populated-home)" in result.stdout
    assert "RESULT: FAIL (python-populated-home)" in result.stdout
    # It says which environment went red, and how to reproduce it by hand.
    assert "scripts/run_tests_in_populated_home.sh" in result.stdout
    assert "tests/test_ambient.py" in result.stdout
    # And it does not pretend the ordinary suite failed.
    assert "FAIL(python):" not in result.stdout


def test_the_arm_never_downgrades_a_verdict(repo: Path) -> None:
    """The mirror image of #2170's baseline check, which only ever downgrades.

    A green arm cannot rescue a red ordinary suite: the ordinary suite runs
    first, and a genuine failure there short-circuits before this arm is reached
    (so it is not even paid for).
    """
    result = _run(repo, FAKE_SUITE_EXIT="1", FAKE_POPULATED_EXIT="0")

    assert result.returncode == 1
    assert "RESULT: FAIL (python)" in result.stdout
    assert "python-populated-home" not in result.stdout


def test_a_red_ordinary_suite_does_not_pay_for_the_arm(
    repo: Path, tmp_path: Path
) -> None:
    """Cost, and attribution: a branch whose ordinary suite is already red learns
    nothing from a second run in a stricter environment, and the report should
    not blame two arms for one defect."""
    argv_log = tmp_path / "populated-argv.log"
    result = _run(repo, FAKE_SUITE_EXIT="1", FAKE_POPULATED_ARGV_LOG=str(argv_log))

    assert result.returncode == 1
    assert not argv_log.exists(), (
        "the populated-$HOME arm ran even though the ordinary suite was red"
    )


# ── the harness's own exit 2 is a FAIL, never a silent pass ─────────────────


def _plant_harness(repo: Path, body: str) -> None:
    """Overwrite the harness in the WORKTREE (not in git).

    The runner reads it from the branch's tree, so an uncommitted overwrite is
    enough — and it keeps the fixture's diff scoped to the one test file.
    """
    _executable(repo / HARNESS_REL, body)


def test_harness_exit_2_is_a_fail_with_warning_not_a_skip(repo: Path) -> None:
    """``run_tests_in_populated_home.sh`` reserves exit 2 for "a knob failed to
    take effect" — the guard itself is broken, so the environment the tests ran
    in was NOT the one it claims to synthesize.

    A green from that is a green that means nothing, which is precisely the
    failure mode #2170 exists to prevent. Every ambiguity in this arm resolves to
    the stricter verdict, so exit 2 is a FAIL that says WHY, not a skip.
    """
    _plant_harness(
        repo,
        "#!/usr/bin/env bash\n"
        "echo 'run_tests_in_populated_home.sh: sqlite3 is STILL resolvable at"
        " /usr/bin/sqlite3 after exclusion — the guard itself is broken' >&2\n"
        "exit 2\n",
    )

    result = _run(repo)

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "FAIL(python-populated-home)" in result.stdout
    assert "A KNOB FAILED TO TAKE EFFECT" in result.stdout
    assert "RESULT: FAIL (python-populated-home)" in result.stdout
    assert "SKIP(python-populated-home)" not in result.stdout
    assert "RESULT: PASS" not in result.stdout


def test_harness_exit_2_is_a_fail_even_if_the_wording_changes(repo: Path) -> None:
    """The exit-2 classification checks ``rc -eq 2`` directly, not only the
    grep against the harness's exact wording — belt-and-braces (#2269 review).

    A harness that exits 2 (or a fork/older copy of it that reserves exit 2 for
    the same "a knob failed to take effect" meaning but phrases it differently)
    must still be classified as the specific knob-failure FAIL, not silently
    fall through to the generic "FAIL in a synthesized fleet $HOME" message.
    """
    _plant_harness(
        repo,
        "#!/usr/bin/env bash\n"
        "echo 'totally different wording, still exit 2' >&2\n"
        "exit 2\n",
    )

    result = _run(repo)

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "FAIL(python-populated-home)" in result.stdout
    assert "A KNOB FAILED TO TAKE EFFECT" in result.stdout
    assert "RESULT: FAIL (python-populated-home)" in result.stdout
    assert "SKIP(python-populated-home)" not in result.stdout
    assert "RESULT: PASS" not in result.stdout


def test_an_unrecognised_nonzero_harness_exit_is_still_a_fail(repo: Path) -> None:
    """Exit 2 is also pytest's "interrupted", and a harness could grow new exit
    codes. Anything non-zero this arm does not specifically recognise still fails
    — the arm has exactly one safe default and this is it."""
    _plant_harness(repo, "#!/usr/bin/env bash\necho 'something unexpected' >&2\nexit 9\n")

    result = _run(repo)

    assert result.returncode == 1
    assert "FAIL(python-populated-home)" in result.stdout
    assert "RESULT: FAIL (python-populated-home)" in result.stdout


# ── the skips, all of them announced ────────────────────────────────────────


def test_a_diff_with_no_python_test_files_skips_the_arm_out_loud(repo: Path) -> None:
    """The acceptance criterion, end-to-end rather than via ``--print-routing``:
    a branch touching no python tests skips the arm and SAYS so, so nobody reads
    a green report and assumes the arm covered them."""
    _git(repo, "checkout", "base", "--", "tests/test_ambient.py")
    (repo / "coord" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "coord-only change")

    result = _run(repo)

    assert result.returncode == 0
    assert "SKIP(python-populated-home)" in result.stdout
    assert "no python test files" in result.stdout
    assert "RESULT: PASS" in result.stdout


def test_a_branch_without_the_harness_skips_rather_than_reddening(repo: Path) -> None:
    """A branch (or repo checkout) predating ``run_tests_in_populated_home.sh``
    cannot run the arm. Failing there would redden every such branch for a file
    it never had; skipping is the only workable answer — but it is announced in
    the verdict stream, not inferred from an absent line."""
    (repo / HARNESS_REL).unlink()

    result = _run(repo)

    assert result.returncode == 0
    assert "SKIP(python-populated-home)" in result.stdout
    assert "absent from this branch's tree" in result.stdout
    assert "RESULT: PASS" in result.stdout


# ── the worked example, with no fakes at all ────────────────────────────────


#: A test that PASSES in an ordinary environment and FAILS in the one the
#: harness synthesizes — the #2174 failure class, reduced to its smallest honest
#: form. It trips knob 1, the one dellserver is structurally incapable of
#: reproducing and therefore the one that hid #2174: the harness seeds
#: ``~/.coord`` with a thin-client ``client.toml`` and a
#: ``coordinator.remote.yml`` cache naming ``fleet-only-repo``.
#:
#: It keys on that fixture repo NAME rather than on "is there a
#: coordinator.yml", because the latter is exactly the machine-dependent
#: question the whole issue is about (dellserver has one, precision does not) —
#: and a fixture whose ambient result depended on the host would reproduce the
#: bug inside the test that is supposed to prove it fixed. No real fleet config
#: contains a repo called ``fleet-only-repo``, so the ambient side of this is
#: true everywhere, on every machine.
_AMBIENT_SENSITIVE_TEST = '''\
import pathlib


def test_the_fleet_config_cache_is_a_real_one():
    """Green under an ordinary $HOME, red under the harness's synthesized one."""
    cache = pathlib.Path.home() / ".coord" / "coordinator.remote.yml"
    assert not (cache.exists() and "fleet-only-repo" in cache.read_text(encoding="utf-8"))
'''


def test_end_to_end_a_really_ambient_sensitive_test_reddens_the_arm(
    tmp_path: Path,
) -> None:
    """#2269's headline acceptance criterion, with NO stubbed interpreter.

    Every other test in this file plants a fake ``python`` so the runner's
    decision logic can be steered cheaply. This one runs a real pytest, twice,
    through the real runner and the real harness — because the claim being made
    is about an ENVIRONMENT, and an environment is exactly the thing a fake
    cannot vouch for. A branch adding this test would have been green on
    dellserver and red in CI's ``populated-home`` job; here it is red at the Test
    gate, on whatever machine happens to be running this suite.

    The runner subprocess is handed a scrubbed ``$HOME`` so this test stays
    hermetic even when it is ITSELF re-run by the arm it is testing — which is
    exactly what this branch's own Test leg does, since this file is a python
    test file in its own diff. Without it the nested run would inherit the outer
    harness's synthesized ``$HOME``, the fixture's ORDINARY suite would go red
    for the same reason its populated one does, and the assertion below would
    fail for a reason that has nothing to do with the code under test.
    """
    import sys

    clean_home = tmp_path / "clean-home"
    clean_home.mkdir()

    r = tmp_path / "real"
    (r / "tests").mkdir(parents=True)
    (r / "scripts").mkdir(parents=True)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "Test")
    (r / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    # An ini file so the fixture repo anchors its OWN pytest rootdir. This is the
    # only test in this file that runs a real pytest inside `tmp_path`, and
    # without an anchor that nested run infers its rootdir by walking UP out of
    # `tmp_path` — so any stray `pyproject.toml`/`conftest.py` left in `$TMPDIR`
    # by some unrelated process becomes an ancestor config of the fixture repo.
    # On a machine carrying a stray `/tmp/conftest.py` copied from THIS repo,
    # the nested run imported it and died with
    # `INTERNALERROR ModuleNotFoundError: No module named 'tests.backends'`
    # before collecting a single fixture test — a red Test gate with no relation
    # to the branch under test. rootdir also fixes confcutdir, so anchoring here
    # cuts ancestor conftest collection off at the fixture repo, exactly as the
    # scrubbed `$HOME` below cuts off ambient fleet state. Committed in the BASE
    # commit on purpose: the runner is diff-scoped, and this must not show up as
    # a changed file.
    (r / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    shutil.copy2(HARNESS, r / HARNESS_REL)
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "initial")
    _git(r, "tag", "base")

    (r / "tests" / "test_ambient_sensitive.py").write_text(
        _AMBIENT_SENSITIVE_TEST, encoding="utf-8"
    )
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "add an ambient-sensitive test")

    # The runner reuses `$WT/.venv/bin/python` when one exists, so pointing it at
    # the interpreter already running this suite skips a ~12s venv build while
    # still being a REAL interpreter with a real pytest. An exec WRAPPER, not a
    # symlink: a venv interpreter reached through a symlink sitting in a
    # directory with no `pyvenv.cfg` resolves the chain down to the BASE python
    # and loses the venv's site-packages — "No module named pytest", and the
    # ordinary arm goes red for a reason that has nothing to do with the code
    # under test. Exec'ing `sys.executable` by its real absolute path keeps the
    # venv machinery intact.
    venv_bin = r / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _executable(
        venv_bin / "python",
        f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n',
    )
    _executable(venv_bin / "coord", _FAKE_COORD)

    result = _run(r, HOME=str(clean_home))

    # The ordinary arm is green — the new test passes in this machine's own
    # environment, which is exactly why the Test gate could not see it before.
    assert "FAIL(python):" not in result.stdout, result.stdout
    assert "PASS(python):" in result.stdout, result.stdout
    # ...and red once the fleet machine's environment is synthesized.
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "FAIL(python-populated-home)" in result.stdout
    assert "RESULT: FAIL (python-populated-home)" in result.stdout
    assert "tests/test_ambient_sensitive.py" in result.stdout
    assert "A KNOB FAILED TO TAKE EFFECT" not in result.stdout
    # Scoped to the diff: the pre-existing test file is not re-run.
    assert "tests/test_ok.py" not in result.stdout


# ── the blast radius (#2269): the other arms are untouched ──────────────────


def test_the_fallback_arm_never_grows_a_populated_home_arm(tmp_path: Path) -> None:
    """``scripts/coord-test-runner.sh`` is the Test gate's engine for EVERY repo
    on the fleet. This arm is this repo's python arm only: it parses pytest's
    file layout and reuses this repo's harness, neither of which an arbitrary
    repo's ``test_command`` has. The fallback routing line must be byte-identical
    to what it was before #2269."""
    r = tmp_path / "other"
    (r / "src").mkdir(parents=True)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "Test")
    (r / "README.md").write_text("init\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "initial")
    _git(r, "tag", "base")
    (r / "src" / "lib.rs").write_text("x\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "src change")

    result = subprocess.run(
        [
            POSIX_BASH, str(SCRIPT), str(r),
            "--base-ref", "base",
            "--repo", "quadraui",
            "--fallback-command", "true",
            "--print-routing",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert result.returncode == 0
    assert result.stdout.strip().splitlines()[-1] == "ROUTING mode=fallback run=1"
    assert "populated" not in result.stdout
