"""#2170 -- the ambient-``$HOME`` regression pin, in the CI-invisible direction.

Six tests passed on ``ubuntu-latest`` (3.12 and 3.13) and failed on *every*
fleet machine, on ``origin/main``, for months. The consequence was not six red
tests: it was that the **Test stage on `precision` could not produce a green
verdict for this repo on any branch**. Every dispatch there returned
``SMOKE: fail``, blamed the branch, and cost a human verdict to adjudicate
(claude-coordinator#2158's Test leg: $0.61, 11m36s, zero signal about the
branch).

WHY CI COULD NOT SEE IT. The familiar version of this bug is "passes on my
machine, fails in CI ⇒ something ambient in ``$HOME``". These three are the
**inverse**: they pass in CI *precisely because* CI's ``$HOME`` is empty, and
fail wherever it isn't. Same class, flipped direction -- and a green
``ubuntu-latest`` matrix is structurally incapable of catching that direction,
because the only environment it ever tests is the empty one.

So the fix cannot be only "fix the three tests" (that clears today's six and
nothing more). It has to include a job that runs them in the environment that
breaks them. That is this file: it drives the three ambient-sensitive targets
in a subprocess whose environment is a *hostile* reproduction of a real fleet
machine --

1. a populated ``~/.coord/`` holding ``client.toml`` (so `coord` is a thin
   client) and a ``coordinator.remote.yml`` cache, and **no**
   ``coordinator.yml`` -- exactly `precision`'s shape;
2. ``sqlite3`` masked off ``$PATH`` -- it is not a provisioned fleet
   dependency and ``[dev]`` extras cannot install a system binary;
3. a ``$TMPDIR`` whose *ancestor* holds a ``pyproject.toml`` with
   ``[tool.pytest.ini_options]``, which is what shifts a nested pytest's
   inferred rootdir and mangles the JUnit ``classname`` the ids are derived
   from.

Each of the three knobs reproduces exactly one of the three original failures,
and each is asserted to be genuinely hostile by
:func:`test_the_hostile_environment_is_actually_hostile` -- without that
second test, a masking helper that quietly stopped masking would leave this
file green for the wrong reason, which is the same silent-green failure mode
the issue is about.

Deliberately NOT a re-run of the whole suite: recursion aside, the value is in
pinning the three known-sensitive targets permanently and cheaply (~10s), not
in doubling every CI run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The whole-suite sweep that applies the same three knobs to an arbitrary
#: command, and which `.github/workflows/test.yml`'s `populated-home` job runs.
POPULATED_HOME_SCRIPT = REPO_ROOT / "scripts" / "run_tests_in_populated_home.sh"

#: The node ids that failed on `precision` and passed in CI, plus the thin-client
#: contract test added alongside the fix. Spelled out one-by-one rather than as
#: whole files so a future reader can map each to its mechanism, and so this
#: pin's runtime stays bounded.
AMBIENT_SENSITIVE_TARGETS = (
    # (1) `sqlite3` off $PATH -- 4 of these 8 died with "command not found".
    "tests/test_deploy_coord_db_backup.py",
    # (2) populated ~/.coord/ ⇒ thin client ⇒ `--config` is not authoritative.
    "tests/test_cross_platform_imports.py::test_coord_config_runs_without_posix_modules",
    "tests/test_cross_platform_imports.py"
    "::test_coord_config_on_a_thin_client_does_not_read_the_given_file",
    # (3) $TMPDIR under an ancestor pytest config ⇒ shifted rootdir ⇒ KeyError.
    "tests/test_acceptance_drivers.py"
    "::TestRunDriverCliPytest::test_runs_real_pytest_and_parses_junit_xml",
)

#: A pytest config file at the root of the fake ``$HOME``, i.e. an *ancestor* of
#: the ``$TMPDIR`` the inner run's ``tmp_path`` is carved out of. This is the
#: whole of knob (3): pytest infers rootdir by walking upward for one of these,
#: and derives each JUnit ``classname`` from the nodeid relative to it.
_ROOTDIR_SHIFTER = 'pyproject.toml'
_ROOTDIR_SHIFTER_BODY = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'

#: Seeded into the fake ``~/.coord/``. The URL is unreachable on purpose: a
#: thin client that cannot reach its daemon must fail loudly rather than fall
#: through to a local file (#1080), so an unreachable daemon is enough to make
#: the thin-client branch observable without standing one up. Port 9 is the
#: IANA discard port -- loopback connects are refused immediately, so nothing
#: here waits out a network timeout or leaves the machine.
_FAKE_CLIENT_TOML = 'board_service = "http://127.0.0.1:9"\n'

#: Stands in for the cache `coord` overwrites on essentially every command.
#: Its contents are never meant to be read by a fixed test -- if any assertion
#: ever matches "fleet-only-repo", something is reading the cache again.
_FAKE_REMOTE_CACHE = """
repos:
  - name: fleet-only-repo
    github: fleet/only
machines:
  - name: fleet-only-machine
    host: fleet-only.tailnet
    repos: [fleet-only-repo]
"""

#: Environment variables that would leak the *outer* pytest run into the inner
#: one. ``PYTEST_ADDOPTS`` is the dangerous one (it would silently prepend
#: flags); the xdist pair and ``PYTEST_CURRENT_TEST`` are dropped for the same
#: "the inner run must be a fresh run" reason.
_LEAKY_PYTEST_ENV = (
    "PYTEST_ADDOPTS",
    "PYTEST_CURRENT_TEST",
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PYTEST_XDIST_TESTRUNUID",
)


def _mask_sqlite3_off_path(mask_dir: Path) -> str:
    """Build a ``$PATH`` that has everything the current one has **except**
    ``sqlite3``, and return it.

    Done as a symlink farm rather than by dropping the directories that contain
    ``sqlite3``, because on a normal Linux box that directory is ``/usr/bin`` --
    dropping it would also take ``bash`` and ``git`` with it, and the resulting
    run would fail for reasons that have nothing to do with the guard under
    test. Masking one *name* is the narrowest edit that reproduces `precision`.
    """
    mask_dir.mkdir(parents=True, exist_ok=True)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        source_dir = Path(entry)
        try:
            items = sorted(source_dir.iterdir())
        except OSError:
            continue  # unreadable / nonexistent PATH entry — as `which` sees it
        for item in items:
            if item.name == "sqlite3":
                continue
            link = mask_dir / item.name
            if link.is_symlink() or link.exists():
                continue  # first PATH entry wins, as real resolution does
            try:
                link.symlink_to(item)
            except OSError:
                pass
    return str(mask_dir)


def _seed_populated_coord_home(home: Path) -> None:
    """Give *home* the ``~/.coord/`` of a real thin client: a ``client.toml``
    naming a board daemon and a ``coordinator.remote.yml`` cache, and no
    ``coordinator.yml``.

    The missing ``coordinator.yml`` is not an oversight — it is the observed
    shape on `precision`, and it is what makes the failure counter-intuitive:
    there is no local config for ``--config`` to be shadowed *by*. The
    shadowing is done by the daemon, over the network.
    """
    coord_dir = home / ".coord"
    coord_dir.mkdir(parents=True, exist_ok=True)
    (coord_dir / "client.toml").write_text(_FAKE_CLIENT_TOML)
    (coord_dir / "coordinator.remote.yml").write_text(_FAKE_REMOTE_CACHE)
    assert not (coord_dir / "coordinator.yml").exists()


def _hostile_env(tmp_path: Path) -> dict[str, str]:
    """The three knobs, applied to a copy of ``os.environ``."""
    home = tmp_path / "hostile-home"
    home.mkdir(parents=True, exist_ok=True)
    _seed_populated_coord_home(home)
    (home / _ROOTDIR_SHIFTER).write_text(_ROOTDIR_SHIFTER_BODY)
    tmpdir = home / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["TMPDIR"] = str(tmpdir)
    env["PATH"] = _mask_sqlite3_off_path(tmp_path / "masked-bin")
    # The bootstrap contract is flag > env > file, and we want the *file* to be
    # the thing that makes this a thin client — that is `precision`'s shape, and
    # it means the fix has to isolate `$HOME`, not just scrub two env vars.
    for var in ("COORD_SERVICE_URL", "COORD_TOKEN", "COORD_CONFIG"):
        env.pop(var, None)
    for var in _LEAKY_PYTEST_ENV:
        env.pop(var, None)
    return env


def test_the_hostile_environment_is_actually_hostile(tmp_path: Path) -> None:
    """The pin's own pin: assert each knob really is set (#2170).

    A masking helper that silently stopped masking, or a seeded ``$HOME`` that
    stopped being discoverable, would make the test below pass for the wrong
    reason — green because the environment went benign, not because the code
    is isolated. That is the identical silent-green failure this whole issue is
    about, so it gets an assertion rather than trust.
    """
    env = _hostile_env(tmp_path)
    home = Path(env["HOME"])

    # (1) sqlite3 is gone — and, just as importantly, the shell and git are not.
    assert shutil.which("sqlite3", path=env["PATH"]) is None
    for still_needed in ("bash", "sh", "git"):
        assert shutil.which(still_needed, path=env["PATH"]) is not None, still_needed

    # (2) `coord` would resolve a board service from this $HOME alone.
    assert (home / ".coord" / "client.toml").is_file()
    assert (home / ".coord" / "coordinator.remote.yml").is_file()
    assert not (home / ".coord" / "coordinator.yml").exists()
    assert "COORD_SERVICE_URL" not in env

    # (3) $TMPDIR sits *below* a pytest config file, which is what shifts a
    #     nested run's rootdir.
    tmpdir = Path(env["TMPDIR"])
    assert tmpdir.is_dir()
    shifter = home / _ROOTDIR_SHIFTER
    assert shifter.is_file()
    assert shifter.parent in tmpdir.parents or shifter.parent == tmpdir.parent


def test_the_populated_home_really_would_make_coord_a_thin_client(
    tmp_path: Path,
) -> None:
    """Knob (2) proven through the product's own resolver, not by inspection.

    ``resolve_board_service`` is the function whose answer decides whether
    ``coord config --config <file>`` reads that file at all. Running it in a
    subprocess under the seeded ``$HOME`` shows it returns a service — i.e. the
    hostile ``$HOME`` is hostile *to `coord`*, not merely to a path assertion.

    A subprocess is required: ``coord.client.COORD_DIR`` is
    ``Path.home() / ".coord"`` evaluated at import time, so an in-process
    ``$HOME`` change would come too late.
    """
    env = _hostile_env(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from coord.client import resolve_board_service as r; "
            "svc = r(); print('SERVICE', svc.url if svc else None)",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "SERVICE http://127.0.0.1:9" in proc.stdout


@pytest.mark.timeout(600)
def test_ambient_sensitive_targets_pass_in_a_populated_home(tmp_path: Path) -> None:
    """THE regression. All three ambient-sensitive targets, run in the hostile
    environment, must come back green (#2170).

    #3796: the nested ``subprocess.run`` call below carries its own
    ``timeout=600`` budget, well past the suite-wide ``--timeout=120``
    CI passes at the outer pytest invocation. Without this marker that
    outer bound would fail this test as "hung" long before its own,
    larger budget ever gets a chance to fire.

    Before the fix this fails three ways at once, one per knob:

    * ``sqlite3: command not found`` from ``deploy/coord-db-backup.sh``,
    * ``coord config --config`` re-fetching from the daemon instead of reading
      the fixture (here: exiting 2 because the daemon is unreachable; on
      `precision`, printing the *fleet's* repos and machines),
    * ``KeyError: 'test_sample::test_pass'`` from a rootdir-mangled JUnit
      ``classname``.

    After it, the sqlite3 tests SKIP with a reason that names the missing
    binary, and the other two pass by isolating themselves instead of by
    inheriting an empty environment.
    """
    env = _hostile_env(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *AMBIENT_SENSITIVE_TARGETS,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        "the ambient-sensitive targets are red under a populated $HOME "
        f"(exit {proc.returncode}) — this is the #2170 regression:\n"
        f"{proc.stdout[-6000:]}\n{proc.stderr[-2000:]}"
    )
    # The sqlite3 tests must SKIP, not silently vanish or pass: a pin that
    # tolerated "collected 0 items" would be satisfied by deleting the file.
    assert "skipped" in proc.stdout, proc.stdout[-2000:]
    # And nothing may have read the remote cache — if the fleet's fake config
    # leaked into an assertion, the isolation is not doing its job.
    assert "fleet-only-repo" not in proc.stdout


# ── scripts/run_tests_in_populated_home.sh ──────────────────────────────────
#
# The Python knobs above are what runs on every machine on every run. The shell
# script is the *whole-suite* sweep -- the thing a human (and CI's
# `populated-home` job) runs to find the NEXT test of this class. It reimplements
# the three knobs in bash because it has to wrap an arbitrary command, which a
# pytest fixture cannot. Duplicated mechanism means it can silently drift into
# masking nothing, so the two tests below drive it for real rather than trusting
# its header -- the same reason
# `test_the_hostile_environment_is_actually_hostile` exists for the Python side.

_ENV_PROBE = (
    'printf "HOME=%s\\n" "$HOME"; '
    'printf "TMPDIR=%s\\n" "$TMPDIR"; '
    'printf "SQLITE3=%s\\n" "$(command -v sqlite3 || echo NONE)"; '
    'printf "GIT=%s\\n" "$(command -v git || echo NONE)"; '
    'printf "BASH=%s\\n" "$(command -v bash || echo NONE)"; '
    'printf "CLIENT_TOML=%s\\n" "$(test -f "$HOME/.coord/client.toml" '
    '&& echo yes || echo no)"; '
    'printf "REMOTE_CACHE=%s\\n" '
    '"$(test -f "$HOME/.coord/coordinator.remote.yml" && echo yes || echo no)"; '
    'printf "LOCAL_CONFIG=%s\\n" "$(test -e "$HOME/.coord/coordinator.yml" '
    '&& echo yes || echo no)"; '
    'printf "SHIFTER=%s\\n" "$(test -f "$HOME/pyproject.toml" && echo yes || echo no)"; '
    'printf "SERVICE_URL=[%s]\\n" "${COORD_SERVICE_URL-unset}"'
)


def _probe_the_script() -> dict[str, str]:
    """Run the sweep script with a command that just reports its environment."""
    proc = subprocess.run(
        [str(POPULATED_HOME_SCRIPT), "bash", "-c", _ENV_PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    return dict(
        line.split("=", 1)
        for line in proc.stdout.splitlines()
        if "=" in line
    )


def test_sweep_script_is_executable_and_self_documenting() -> None:
    """Committed +x and with a `--`-free contract: CI invokes it directly."""
    assert POPULATED_HOME_SCRIPT.is_file()
    assert os.access(POPULATED_HOME_SCRIPT, os.X_OK), (
        f"{POPULATED_HOME_SCRIPT} is not executable -- CI runs it as a bare "
        "command, so the mode bit is part of the contract"
    )
    header = POPULATED_HOME_SCRIPT.read_text()
    # The three knobs must each stay named in the header. This is not
    # prose-policing: a future edit that drops a knob but leaves the header
    # intact is caught by the behavioural test below, whereas one that drops
    # the *explanation* leaves the next reader unable to tell hostile-on-purpose
    # from broken -- which is how a masking bug survives.
    for knob in ("client.toml", "sqlite3", "TMPDIR"):
        assert knob in header, knob


@pytest.mark.timeout(600)
def test_sweep_script_applies_all_three_knobs() -> None:
    """Drive the script for real and check each knob landed (#2170).

    A sweep that quietly stopped masking would report a green full suite that
    proves nothing -- the identical silent-green failure mode this issue is
    about -- so the script's own guard (`exit 2`) and this test both exist.

    #3796: marked past the outer `--timeout=120` CI passes for the same
    reason as `test_ambient_sensitive_targets_pass_in_a_populated_home` --
    `_probe_the_script()`'s own nested `subprocess.run` already budgets 300s.
    """
    env = _probe_the_script()

    # (1) the seeded $HOME is thin-client shaped, with no local config to be
    #     shadowed by -- exactly `precision`.
    assert env["CLIENT_TOML"] == "yes"
    assert env["REMOTE_CACHE"] == "yes"
    assert env["LOCAL_CONFIG"] == "no"
    assert env["SERVICE_URL"] == "[unset]", (
        "the thin client must come from the FILE, not the env var -- that is "
        "the shape that forces the code under test to isolate $HOME"
    )
    assert env["HOME"] != str(Path.home()), "the real $HOME leaked through"

    # (2) sqlite3 is gone; the shell and git are not.
    assert env["SQLITE3"] == "NONE"
    assert env["GIT"] != "NONE"
    assert env["BASH"] != "NONE"

    # (3) $TMPDIR sits below the rootdir shifter.
    assert env["SHIFTER"] == "yes"
    tmpdir = Path(env["TMPDIR"])
    assert Path(env["HOME"]) in tmpdir.parents or Path(env["HOME"]) == tmpdir.parent
