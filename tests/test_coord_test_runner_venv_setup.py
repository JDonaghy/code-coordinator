"""`scripts/coord-test-runner.sh`'s python-arm SETUP classification.

The python arm has to build the environment it runs the suite from
(`$WT/.venv` + `.[dev]`) before a single test executes. Three distinct things
can go wrong there, and they are not the same verdict:

1. **`python3 -m venv` fails.** That step reads nothing from the branch — it
   needs an interpreter and a writable directory — so no branch can cause it
   (the fleet shape is `ensurepip` exiting 1 after a concurrent run removed
   the worktree underneath it). The suite never ran, so this is INFRA: exit 3,
   `RESULT: INFRA`, and `coord.revalidate.is_infrastructure_failure` reads the
   marker as "nothing was learned about the branch".
2. **`pip install -e .[dev]` fails.** This step DOES read the branch — its
   `pyproject.toml`, its dependency pins — so a branch can genuinely break it
   and it must stay a plain FAIL. This is the half that keeps the gate able to
   fail: were every setup failure called INFRA, a branch with broken packaging
   would be unreportable.
3. **Setup reports success but the venv still cannot run pytest.** Asserting
   the environment is usable because no step raised is the unverified kind of
   verdict; the arm confirms it with `python -m pytest --version` and splits
   the blame by which side built the venv — a leftover `.venv` from an
   interrupted earlier run is INFRA, a venv this run installed `.[dev]` into
   is the branch's dev extra.

Every case is asserted on the OUTPUT and the report file, not the exit code
alone: the message is what a fix worker is dispatched against.

The interpreter is a shell shim, so these run in milliseconds and never build
a real venv. It dispatches on its own basename, so the `python`/`pip`/`coord`
entry points it "creates" inside the fake venv are the same script.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_coord_test_runner_toolchain import (
    BASH,
    SCRIPT,
    _mirror_bin_dir,
    _repo,
)

#: A `python3` that fakes `-m venv`, `-m pytest` and the console scripts a
#: created venv would contain. Every branch is switched by an environment
#: knob so one shim covers all four scenarios, and `$FAKE_PY_LOG` records
#: every invocation for tests that need to prove a step was (not) reached.
_SHIM = r"""#!/bin/sh
printf '%s %s\n' "$(basename "$0")" "$*" >>"$FAKE_PY_LOG"
case "$(basename "$0")" in
    pip|pip3)
        if [ -n "${FAKE_PIP_FAIL:-}" ]; then
            echo "ERROR: Could not build wheels for code-coordinator" >&2
            exit 1
        fi
        exit 0
        ;;
    coord)
        exit 0
        ;;
esac
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    if [ -n "${FAKE_VENV_FAIL:-}" ]; then
        echo "Error: Command '[$3/bin/python3.12, -m, ensurepip, --upgrade, --default-pip]' returned non-zero exit status 1." >&2
        exit 1
    fi
    mkdir -p "$3/bin" || exit 1
    for name in python python3 pip coord; do
        cp "$FAKE_PY_SELF" "$3/bin/$name" || exit 1
    done
    exit 0
fi
if [ "$1" = "-c" ]; then
    case "$2" in
        *pytest*)
            if [ -n "${FAKE_PYTEST_MISSING:-}" ]; then
                echo "ModuleNotFoundError: No module named 'pytest'" >&2
                exit 1
            fi
            exit 0
            ;;
        *xdist*) exit 1 ;;
        *) exit 0 ;;
    esac
fi
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
    exit 0
fi
exit 1
"""


@pytest.fixture
def shim_bin(tmp_path: Path) -> Path:
    """A daemon-shaped PATH whose only interpreter is the shim."""
    bin_dir = _mirror_bin_dir(tmp_path / "bin", omit_prefixes=("python",))
    shim = bin_dir / "python3"
    shim.write_text(_SHIM)
    shim.chmod(0o755)
    return bin_dir


@pytest.fixture
def py_repo(tmp_path: Path) -> Path:
    """A repo whose diff vs `base` touches coord/**, so routing picks pytest.

    No test file in the diff, so the populated-$HOME arm skips itself and the
    setup classification under test is the only thing in the verdict.
    """
    return _repo(tmp_path, "coord/foo.py")


def _run(
    repo: Path,
    tmp_path: Path,
    bin_dir: Path,
    report: Path | None = None,
    **knobs: str,
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": str(bin_dir),
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "FAKE_PY_LOG": str(tmp_path / "shim.log"),
        "FAKE_PY_SELF": str(bin_dir / "python3"),
    }
    env.update(knobs)
    args = [str(repo), "--base-ref", "base", "--repo", "claude-coordinator"]
    if report is not None:
        args += ["--report", str(report)]
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


# ── (1) a venv that could not be created is INFRA, not a red branch ─────────


def test_venv_creation_failure_is_infra_not_a_branch_failure(
    py_repo: Path, tmp_path: Path, shim_bin: Path
) -> None:
    result = _run(py_repo, tmp_path, shim_bin, FAKE_VENV_FAIL="1")

    out = result.stdout + result.stderr
    assert result.returncode == 3, out
    assert "ENVIRONMENT BROKEN(python)" in out
    assert "RESULT: INFRA" in out
    assert "RESULT: FAIL" not in out
    assert "FAIL(python)" not in out
    # The blame must not land on the step that never ran.
    assert "could not install .[dev]" not in out


def test_venv_creation_failure_carries_the_stderr_that_explains_it(
    py_repo: Path, tmp_path: Path, shim_bin: Path
) -> None:
    """"Environment problem" with no output behind it is an unverifiable
    claim — the captured `ensurepip` error is the evidence for the verdict."""
    result = _run(py_repo, tmp_path, shim_bin, FAKE_VENV_FAIL="1")

    out = result.stdout + result.stderr
    assert "ensurepip" in out
    assert "non-zero exit status 1" in out
    assert "python3 -m venv` failed" in out
    assert "does not read the branch's content" in out


def test_venv_creation_failure_classification_reaches_the_report(
    py_repo: Path, tmp_path: Path, shim_bin: Path
) -> None:
    """`--report` is what the Test gate reads back, so the classification has
    to survive into it rather than existing only on the console."""
    report = tmp_path / "report.txt"
    _run(py_repo, tmp_path, shim_bin, report=report, FAKE_VENV_FAIL="1")

    body = report.read_text()
    assert "ENVIRONMENT BROKEN(python)" in body
    assert "RESULT: INFRA" in body
    assert "RESULT: FAIL" not in body
    assert "ensurepip" in body


def test_venv_creation_failure_reads_as_infra_to_the_coordinator(
    py_repo: Path, tmp_path: Path, shim_bin: Path
) -> None:
    """One question, one answer: `coord.revalidate.is_infrastructure_failure`
    is what every coordinator-side consumer asks "did the suite actually run?"
    — so the runner's wording has to satisfy THAT function, not merely look
    infra-flavoured to a human reader."""
    from coord.revalidate import is_infrastructure_failure

    result = _run(py_repo, tmp_path, shim_bin, FAKE_VENV_FAIL="1")

    assert is_infrastructure_failure(
        result.returncode, result.stdout + result.stderr
    ), result.stdout + result.stderr


# ── (2) the other direction: a branch CAN still fail setup ──────────────────


def test_failed_dev_install_is_still_a_branch_failure(
    py_repo: Path, tmp_path: Path, shim_bin: Path
) -> None:
    """`pip install -e .[dev]` reads the branch's own packaging, so this one
    must stay a FAIL — a gate that called every setup failure INFRA could not
    report a branch with a broken `pyproject.toml` at all."""
    result = _run(py_repo, tmp_path, shim_bin, FAKE_PIP_FAIL="1")

    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "FAIL(python): could not install .[dev]" in out
    assert "RESULT: FAIL" in out
    assert "RESULT: INFRA" not in out
    assert "ENVIRONMENT BROKEN" not in out
    # The pip output is what tells the two causes apart, so it is reported.
    assert "Could not build wheels" in out


# ── (3) setup "succeeded" but the venv cannot run the suite ────────────────


def test_a_leftover_unusable_venv_is_infra(
    py_repo: Path, tmp_path: Path, shim_bin: Path
) -> None:
    """A `.venv` left behind by an interrupted earlier run — `bin/python`
    present, nothing installed — is skipped by the "already built" guard, so
    nothing installs into it and the arm must not infer it is usable."""
    venv_bin = py_repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    for name in ("python", "pip"):
        entry = venv_bin / name
        entry.write_text(_SHIM)
        entry.chmod(0o755)

    result = _run(py_repo, tmp_path, shim_bin, FAKE_PYTEST_MISSING="1")

    out = result.stdout + result.stderr
    assert result.returncode == 3, out
    assert "ENVIRONMENT BROKEN(python)" in out
    assert "leftover from an interrupted setup" in out
    assert "No module named 'pytest'" in out
    assert "RESULT: FAIL" not in out
    # Nothing was installed into it this run, so pip must not be blamed.
    assert "could not install .[dev]" not in out


def test_a_freshly_installed_venv_without_pytest_is_the_branchs_failure(
    py_repo: Path, tmp_path: Path, shim_bin: Path
) -> None:
    """The same unusable venv, blamed the other way: `.[dev]` installed
    cleanly into a venv this run built and pytest is still missing, which is a
    statement about the branch's dev extra, not about the machine."""
    result = _run(py_repo, tmp_path, shim_bin, FAKE_PYTEST_MISSING="1")

    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "this branch's dev extra does not provide a usable pytest" in out
    assert "RESULT: FAIL" in out
    assert "RESULT: INFRA" not in out
    assert "ENVIRONMENT BROKEN" not in out


# ── (4) the positive control: a usable venv is not reclassified ────────────


def test_a_working_setup_still_runs_the_suite_green(
    py_repo: Path, tmp_path: Path, shim_bin: Path
) -> None:
    """The confirmation step must not turn healthy setups into verdicts of
    their own: with every shim succeeding the arm reports PASS and the venv
    check leaves no trace in the verdict."""
    result = _run(py_repo, tmp_path, shim_bin)

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "RESULT: PASS" in out
    assert "ENVIRONMENT BROKEN" not in out
    assert "FAIL(python)" not in out
    # ...and the confirmation really was executed against the venv.
    log = (tmp_path / "shim.log").read_text()
    assert "python -c import pytest" in log
