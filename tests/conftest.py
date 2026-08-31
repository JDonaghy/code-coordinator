"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coord import github_ops

# Captured at import time — the pristine ``subprocess.run`` / ``_gh``, before
# any test or fixture has monkeypatched either. ``_no_live_gh`` below compares
# the live ``subprocess.run`` against this reference to tell "a test mocked
# the subprocess boundary itself" (delegate to the real ``_gh``, which will
# hit that mock) apart from "nothing mocked anything" (raise instead of
# shelling out for real). ``subprocess`` is a singleton module, so this is the
# same object whether reached via ``subprocess.run`` or
# ``coord.github_ops.subprocess.run`` — the two spellings tests use
# interchangeably to patch it.
_REAL_SUBPROCESS_RUN = subprocess.run
_REAL_GH = github_ops._gh

# #2615: ``$HOME`` as it stood when THIS conftest was first imported — i.e.
# before any test's own fixture has had a chance to redirect it. On a
# developer's own machine that is the real home directory; under
# ``scripts/run_tests_in_populated_home.sh`` (#2170) it is the one throwaway
# thin-client directory the wrapper script exports for the WHOLE pytest
# session. Either way, it is "the ambient value nobody's individual test
# fixture is responsible for" — which is exactly what ``_no_real_pause_store``
# below needs to recognise and redirect away from. See that fixture's
# docstring for why a plain ``pwd``-based "real home" check (the pre-#2615
# version of this) doesn't cover the populated-home case.
_AMBIENT_HOME_AT_COLLECTION = Path(
    os.environ.get("HOME") or os.path.expanduser("~")
).resolve()


def _resolve_posix_bash() -> str:
    """#2727: resolve a real POSIX ``bash`` deliberately rather than trusting
    bare ``"bash"`` + ``PATH`` order.

    On the ``windows-latest`` GitHub runner, bare ``bash`` resolves via
    ``PATH`` to ``C:\\Windows\\System32\\bash.exe`` -- the WSL launcher, not
    a POSIX shell. No distro is installed there, so it prints its banner (in
    UTF-16LE) to stderr and exits non-zero; any test that shells out to a
    REAL ``.sh`` script under test would then be reading that banner instead
    of ever running the script.

    Git Bash ships on the same runners at
    ``C:\\Program Files\\Git\\bin\\bash.exe``. Prefer whatever ``bash``
    ``PATH`` resolution finds UNLESS it is the WSL launcher (identified by
    living under a ``System32`` directory), then fall back to that known
    Git-Bash install path.

    On POSIX this is a no-op: ``shutil.which("bash")`` never resolves to a
    path containing ``System32``, so every call site keeps using exactly the
    ``bash`` it always did.
    """
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if git_bash.exists():
        return str(git_bash)
    return found or "bash"


#: A real POSIX ``bash`` executable, resolved once at collection time (#2727).
#: Every test that shells out to a REAL ``.sh`` script under test should
#: launch it via this constant instead of the bare string ``"bash"``, so the
#: subprocess is never the WSL launcher on a Windows runner.
POSIX_BASH = _resolve_posix_bash()


def pytest_configure(config):
    """#2884: validate ``COORD_TEST_BACKEND`` once, before anything is collected.

    The backend is consumed by an *autouse* fixture, so a bad selection (typo,
    missing ``psycopg``, unreachable server) would otherwise surface as one
    fixture ERROR per test — thousands of identical tracebacks burying the
    thing a second-backend run exists to produce, namely the list of genuine
    assertion failures. See :func:`tests.backends.preflight`.

    A no-op on the default SQLite path: it neither imports a driver nor opens
    a socket, so a plain ``pytest`` is unaffected.
    """
    from tests.backends import preflight

    preflight()


@pytest.fixture(autouse=True)
def _non_terminal_work(monkeypatch):
    """#522: default ALL work to NON-terminal so any test that dispatches a
    review/fix never shells out to ``gh`` through the chokepoint guard
    (``dispatch_review`` / the auto-loop).  Tests exercising the guard re-patch
    ``coord.github_ops.work_is_terminal`` (or ``issue_is_closed`` /
    ``pr_is_merged``) to opt in.  ``test_github_ops`` tests the real helpers
    via captured references, so this module-attr stub does not affect them.
    """
    monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _no_board_service(monkeypatch, tmp_path):
    """#584/#590: keep board-service resolution UNSET by default so tests never
    pick up the dev machine's real ``~/.coord/client.toml`` or
    ``COORD_SERVICE_URL`` and try to hit a live daemon.  Tests that exercise the
    thin-client path opt in by monkeypatching ``coord.client.resolve_board_service``
    (or ``CLIENT_TOML`` / the env vars) themselves — that runs after this
    autouse fixture, so it wins.
    """
    import coord.client as _cc

    monkeypatch.delenv("COORD_SERVICE_URL", raising=False)
    monkeypatch.delenv("COORD_TOKEN", raising=False)
    monkeypatch.setattr(_cc, "CLIENT_TOML", tmp_path / "absent-client.toml")


@pytest.fixture(autouse=True)
def _fresh_resource_route_support():
    """#1946: forget which daemons support the resource routes between tests.

    ``coord.board_service`` memoizes, per (daemon url, route), whether a
    resource route answered 404/405 — i.e. whether that daemon predates
    #1944 — so a thin client pays the doomed round trip once instead of on
    every write.  The memo is module-level, so without this a test that
    exercises the deploy-lag fallback would silently un-migrate every later
    test that reuses the same fake daemon URL.
    """
    from coord import board_service

    board_service.reset_resource_route_support()
    yield
    board_service.reset_resource_route_support()


@pytest.fixture(autouse=True)
def _no_real_webapp_bundle(monkeypatch, tmp_path):
    """#2009: never let the HOST's live webapp bundle change a test's answer.

    ``coord.dashboard.server.WEBAPP_DIST`` used to default to a path inside
    the installed package (``coord/dashboard/webapp/dist``), which was absent
    in any checkout where nobody had run ``npm run build`` — so most tests
    were isolated from it by accident, and only ``tests/test_dashboard.py``
    bothered to patch it explicitly.

    That accident is gone. The webapp moved to the ``coord-web`` repo, so the
    default is now ``~/coord-web-dist`` — the symlink
    ``coord-web-dist-build.sh`` publishes, which *does* exist on exactly the
    machines most likely to run this suite (the daemon host, a dev box with
    the timer installed). Without this fixture, ``build_app()`` on those
    machines registers the SPA static routes and serves the real bundle's
    ``index.html`` at ``/``, while the same test on a CI runner serves the
    legacy dashboard — a green-here/red-there split that has nothing to do
    with the code under test.

    Tests that care about bundle behaviour patch ``WEBAPP_DIST`` themselves
    (or pass ``dist_path=``); that runs after this autouse fixture, so it
    wins — same convention as ``_no_board_service`` above.
    """
    try:
        import coord.dashboard.server as _server
    except ImportError:
        # The dashboard lives behind the optional `[server]` extra (#1237);
        # a client-only install has no dashboard to isolate.
        return

    monkeypatch.setattr(_server, "WEBAPP_DIST", tmp_path / "absent-webapp-dist")


@pytest.fixture(autouse=True)
def _no_agent_health_probe(monkeypatch):
    """#904: default the reviewer /health pre-filter to fail-open (``None``)
    so tests that don't pass ``health_checker=`` to ``dispatch_review``
    (nearly all of them) never make a real ``httpx.get(".../health")`` call.
    Previously this fell through to the real ``_fetch_agent_advertised_repos``,
    which resolved fast locally (NXDOMAIN) but made the suite's default
    behavior depend on network/DNS timing rather than being fully hermetic.
    Tests exercising the health-filter itself pass an explicit
    ``health_checker=`` to ``dispatch_review``, which takes priority over this
    default and is unaffected by this stub.
    """
    monkeypatch.setattr(
        "coord.review._fetch_agent_advertised_repos", lambda *a, **k: None
    )


@pytest.fixture(autouse=True)
def _no_assign_repo_drift_probe(monkeypatch):
    """#2219: default `coord assign`'s live-agent repo-capability cross-check
    (``coord.commands.dispatch._repo_capability_refusal``) to fail-open
    (``None`` — never refuse) so tests that don't care about it don't make a
    real ``httpx.get(".../health")`` call keyed off whatever bogus
    ``host.tailnet`` hostnames the fixture configs use. Mirrors
    ``_no_agent_health_probe`` (#904) for the same reason: the suite's
    default behavior must not depend on network/DNS timing. Tests exercising
    the drift check itself monkeypatch this function to return a message
    (or call the real one against a stubbed ``coord.network.check_machine``),
    which takes priority over this default and is unaffected by it.
    """
    monkeypatch.setattr(
        "coord.commands.dispatch._repo_capability_refusal", lambda *a, **k: None
    )


@pytest.fixture(autouse=True)
def _no_worktree_writable_deny_scan(monkeypatch):
    """#1445 review: keep `check_worktree_writable`'s deny-rule scan from
    reading whatever `~/.claude/settings.json` happens to exist on the
    machine running pytest.

    `AgentServer.assign()` and `coord diagnose --orphan-worktrees`
    (`coord/commands/status.py`) both call `check_worktree_writable()` /
    `find_blocking_deny_rule()` with no `settings_files` override in
    production, which resolves to the real `Path.home() / ".claude" /
    "settings.json"`. Left unpatched, every existing `.assign()`-based test
    in `test_agent.py` (none of which pass an explicit override) would
    implicitly depend on that file's contents — exactly the class of bug
    `_no_board_service` and `_no_agent_health_probe` above already exist to
    prevent, and the fleet already has this exact deny-rule shape on
    dellserver (#1445 itself). Tests exercising the scan directly pass their
    own `settings_files=[...]` (or an explicit
    `worktree_writable_settings_files=` to `AgentServer(...)`), which bypasses
    this default entirely and is unaffected.
    """
    monkeypatch.setattr("coord.agent._default_deny_settings_files", lambda: [])


@pytest.fixture(autouse=True)
def _no_real_usage_probe(monkeypatch):
    """#1466: default the Max-plan usage-window probe to a real subprocess
    NEVER running in tests.  ``coord approve``'s pre-check and
    ``coord.drive.preflight`` (via an injected ``usage_prober=``, so it's
    unaffected by this fixture) call ``coord.usage_limits.get_plan_limits``,
    which shells out to ``claude -p "/usage"`` — on a machine that has
    ``claude`` on PATH (every dev/agent box in this fleet does) that is a
    REAL network call, exactly the class of non-hermetic dependency
    ``_no_agent_health_probe`` above already exists to prevent. Stubbed to
    ``status="unknown"`` — the gate's own fail-open behaviour — so the
    default is silent and identical to "no probe available" everywhere
    except the small number of tests exercising the gate directly, which
    monkeypatch ``coord.usage_limits.get_plan_limits`` (or pass their own
    ``usage_prober=``) themselves, taking priority over this default.
    """
    from coord.usage_limits import PlanLimits

    monkeypatch.setattr(
        "coord.usage_limits.get_plan_limits",
        lambda *a, **k: PlanLimits(status="unknown"),
    )


@pytest.fixture(autouse=True)
def _no_live_gh(monkeypatch):
    """#1484: default every ``coord.github_ops`` helper to fail exactly as it
    would on a host where ``gh`` isn't on PATH (raise ``GhError``, a
    ``RuntimeError`` subclass) instead of shelling out to a real, live,
    authenticated ``gh`` subprocess — regardless of whether ``gh`` happens to
    be installed on the machine running pytest.

    Found via #1472 (elitebook lacked ``gh`` on PATH and ~98 tests that
    inject every *other* dependency still crashed reaching a live ``gh``) and
    confirmed by direct measurement while fixing #1484: running the full
    suite with a real, authenticated ``gh`` on PATH shows ~280 tests reaching
    ``coord.github_ops._gh`` for real (mostly resolving fast via 404s against
    the fixture repo names, but a live authenticated network round-trip all
    the same) — this is the general form of the seam
    ``test_dispatch_review_captures_branch_sha`` illustrates: tests that
    inject every DI seam a function exposes can still fall through an
    uninjected one straight to a real subprocess.

    Mirrors ``_non_terminal_work`` / ``_no_agent_health_probe`` /
    ``_no_real_usage_probe`` above: the seam most callers already treat as
    best-effort/fail-open (``pr_diff`` -> None, ``work_is_terminal`` ->
    False, etc. — see the many ``except RuntimeError`` guards in
    ``coord/github_ops.py`` and its callers) gets a safe default here, and
    tests exercising real ``gh`` behavior inject their own fake via the DI
    parameter the caller already exposes (``pr_lookup``,
    ``branch_sha_fetcher``, ``diff_fetcher``, ...) or monkeypatch
    ``coord.github_ops._gh`` directly themselves — either takes priority
    over this default since it runs after fixture setup.

    A large minority of tests (``tests/test_github_ops.py`` and friends —
    ``test_milestone_seam.py``, ``test_cli_milestone_assign.py``,
    ``test_coord_test.py``, ``test_cli_queue.py``, ``test_cli_track.py``,
    ...) instead mock one level deeper, at ``subprocess.run`` itself, to
    exercise ``_gh()``'s *real* body (its argv building, its
    FileNotFoundError/TimeoutExpired/non-zero-exit handling, #1483). A flat
    ``_gh`` replacement would shadow that real body and break every one of
    them. So the replacement below only raises when ``subprocess.run`` is
    STILL the pristine function captured at collection time — i.e. nothing
    in the test has mocked the subprocess boundary; when a test has (either
    spelling: ``subprocess.run`` or ``coord.github_ops.subprocess.run`` — the
    same singleton module attribute either way), it delegates to the real
    ``_gh``, which then runs against that mock exactly as the test intends.
    """

    def _gh_guard(*args, **kwargs):
        if subprocess.run is _REAL_SUBPROCESS_RUN:
            raise github_ops.GhError(
                f"gh {' '.join(args)} reached the real coord.github_ops._gh() "
                "seam from a test (#1484). Inject the dependency the caller "
                "already exposes (pr_lookup, branch_sha_fetcher, "
                "diff_fetcher, ...), monkeypatch coord.github_ops._gh "
                "directly, or mock subprocess.run to exercise _gh()'s own "
                "body — relying on a live gh subprocess is not allowed."
            )
        return _REAL_GH(*args, **kwargs)

    monkeypatch.setattr(github_ops, "_gh", _gh_guard)


@pytest.fixture(autouse=True)
def _interactive_stdin_is_tty(monkeypatch):
    """#2086: ``coord assign --interactive`` now refuses up front when
    stdin is not a TTY (``coord.commands.dispatch._stdin_is_tty()``) — a
    human-attended session has no operator to drive the paste/attach
    handshake without one. Under pytest, real stdin is essentially never a
    TTY, but the large majority of ``--interactive`` dispatch tests
    (``test_cli_assign.py``, ``test_cli_assign_interactive_remote.py``,
    ...) mock out the actual launcher
    (``launch_human_attended_interactive`` / ``_launch_via_tmux``) entirely
    and are modelling an operator sitting at a real terminal, not the
    headless/no-TTY failure mode this gate exists to catch. Default to
    ``True`` here — mirroring every other "safe default, tests exercising
    the real thing opt out" fixture above — so those tests keep exercising
    what they were written to exercise.

    Patches the ``_stdin_is_tty()`` seam rather than ``sys.stdin.isatty``
    directly: these tests drive the CLI through
    ``click.testing.CliRunner.invoke()``, which swaps in its own
    (permanently non-TTY) stdin object for the duration of the call — a
    pre-``invoke()`` patch of the OLD ``sys.stdin`` object's ``isatty``
    attribute has no effect on CliRunner's replacement. The handful of
    tests for the refusal itself patch
    ``coord.commands.dispatch._stdin_is_tty`` back to ``False`` (or, for
    the pre-existing non-TTY review/test-verdict relay paths in
    ``coord/commands/review.py`` — which are NOT reached through
    CliRunner — monkeypatch ``sys.stdin.isatty`` directly); either runs
    after this fixture and wins.
    """
    monkeypatch.setattr("coord.commands.dispatch._stdin_is_tty", lambda: True)


def output_and_stderr(result) -> str:
    """CLI text across click versions: newer click separates stderr; older
    mixes it into .output and raises on .stderr access."""
    try:
        err = result.stderr or ""
    except ValueError:
        err = ""
    return result.output + err


# ── Portable fake-worker helpers (#2725) ─────────────────────────────────
#
# ``AgentServer.assign()`` spawns whatever argv ``worker_command`` returns as
# a REAL child process (``coord/agent.py``'s ``subprocess.Popen``). Fixtures
# across the suite stood in for the real ``claude -p`` worker with
# hand-rolled POSIX argvs — ``["/bin/true"]``, ``["/bin/sh", "-c", script]``,
# or a ``#!/bin/sh`` shebang script executed directly as argv[0]. All three
# shapes fail to spawn on Windows (``[WinError 193] %1 is not a valid Win32
# application``) since there is no POSIX shell or shebang interpreter on
# PATH there. ``sys.executable`` — the interpreter already running pytest —
# is guaranteed present on every platform, so every fake worker below routes
# through it instead. Import these from ``conftest`` rather than
# reintroducing a POSIX-only literal at a new call site.

NOOP_WORKER_ARGV: list[str] = [sys.executable, "-c", ""]
"""Portable replacement for the POSIX-only ``["/bin/true"]`` no-op worker:
spawns a real child process that does nothing and exits 0."""


def py_worker_argv(script: str) -> list[str]:
    """Portable replacement for ``["/bin/sh", "-c", script]``.

    ``script`` is an inline PYTHON snippet (not shell) executed via
    ``sys.executable -c``. Translate the shell script's observable
    behaviour (stdout, exit code, stdin reads, file writes) into equivalent
    Python — e.g. ``"echo ok"`` -> ``"print('ok')"``, ``"exit 7"`` ->
    ``"import sys; sys.exit(7)"``, ``"read line; echo $line"`` ->
    ``"import sys; print(sys.stdin.readline().rstrip(chr(10)))"``.
    """
    return [sys.executable, "-c", script]


def write_fake_worker_script(path: Path, script: str) -> Path:
    """Write a portable stand-in for a ``#!/bin/sh`` shebang fake-worker
    script at `path` (conventionally ``tmp_path / "fake-claude.py"`` — note
    the ``.py`` suffix). `script` is Python, run via ``sys.executable``, so
    the file needs no shebang line and no execute bit.

    Returns `path` unchanged, for chaining straight into an argv:
    ``[sys.executable, str(write_fake_worker_script(stub, script))]``.
    Callers that previously asserted ``captured_argv[0] == str(stub)``
    (checking that a *specific binary* — e.g. a wire-resolved provider
    binary — was the one actually executed) must update that assertion:
    with this shape ``argv[0]`` is ``sys.executable`` and the stub path is
    ``argv[1]``, so assert ``str(stub) in captured_argv`` (or
    ``captured_argv[1] == str(stub)``) instead — the intent (this exact
    binary ran, not some other one) is unchanged.
    """
    path.write_text(script)
    return path


def noop_default_worker_command(spec, **kwargs) -> list[str]:
    """Portable replacement for
    ``default_worker_command(spec, binary="/bin/true")``.

    Several tests dispatch through the REAL ``coord.agent.default_worker_command``
    (to exercise its actual flag-building — ``--disallowedTools``, deny-list
    patterns, ``--allowedTools``, etc. — end to end) while substituting
    ``/bin/true`` for the real ``claude`` binary so nothing external actually
    runs. ``default_worker_command`` returns ``[binary, "-p", briefing, ...
    many more flags...]`` with ``binary`` fixed at position 0, so we can't
    just swap in ``sys.executable`` there — followed by all those flags, the
    interpreter would try to parse ``-p`` etc. as ITS OWN options and fail.

    Instead this builds the real argv (the ``binary=`` value passed to
    ``default_worker_command`` is irrelevant here — it never actually runs)
    and replaces position 0..2 with ``sys.executable -c "<exit 0>"``.
    ``python -c CMD`` terminates python's own option parsing at CMD — every
    argument after it (the real ``-p``, ``--disallowedTools``, ...) becomes
    the script's OWN ``sys.argv``, not a python flag, so it's inert.  The
    logged header (``shlex.join(argv)``, see ``AgentServer._spawn``) still
    contains every flag verbatim after the substituted prefix, so assertions
    like ``"--disallowedTools" in header`` are unaffected.
    """
    from coord.agent import default_worker_command  # noqa: PLC0415

    argv = default_worker_command(spec, binary="unused-noop-binary", **kwargs)
    return [sys.executable, "-c", "import sys; sys.exit(0)", *argv[1:]]


VALID_CONFIG = """\
repos:
  - name: api
    github: acme/api
    depends_on: [shared]
  - name: shared
    github: acme/shared

machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [api, shared]
  - name: server
    host: server.tailnet
    capabilities: [python, docker]
    repos: [api]
"""


@pytest.fixture
def valid_config_yaml() -> str:
    return VALID_CONFIG


@pytest.fixture
def valid_config_path(tmp_path: Path, valid_config_yaml: str) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(valid_config_yaml)
    return p


@pytest.fixture(autouse=True)
def _no_real_agent_venv(monkeypatch, tmp_path):
    """#1241: default `coord.agent_app._venv_dir()` to a per-test tmp
    directory so a test exercising the (unmocked) blue/green update path
    can never resolve to — and mutate or delete — the real
    ``~/.coord-venv`` on the machine running pytest.

    This bit for real during development of #1241: a test hitting
    ``POST /update`` without mocking ``coord.agent_update.perform_update``
    migrated the dev machine's own live agent venv into the blue/green
    symlink layout, and a second such test then ``rmtree``'d the slot that
    turned out to hold the original install — breaking every *new*
    ``coord`` invocation on that host (the already-running agent process
    kept working off its already-open files, but its systemd unit would
    have failed on its next restart). Tests exercising the real swap logic
    point ``COORD_VENV_DIR`` at their own ``tmp_path`` explicitly (or
    mock ``coord.agent_update.perform_update``/``subprocess.run``
    outright); this default only protects tests that forgot to.
    """
    monkeypatch.setenv("COORD_VENV_DIR", str(tmp_path / "unused-coord-venv"))


@pytest.fixture
def sacrificial_venv_root(monkeypatch, tmp_path):
    """A throwaway blue/green root for tests that must drive the REAL
    upgrade path (#2121 item 3).

    ``_no_real_agent_venv`` above is a *default*: it protects tests that
    forgot to isolate themselves, and — as its own docstring says — nothing
    more. #2121 is what happens when that is the only control: a live
    ``coord-agent``'s venv colour was rebuilt underneath it, and no deploy
    path claimed responsibility. So ``coord.agent_update`` now *asserts*
    the target is not the live install (``assert_not_live_install``, the
    same shape as ``coord.db``'s ``ProductionDatabaseGuardError``, #1960),
    That guard has no bypass, by design — so this fixture is not a way
    *past* it, it is the alternative it exists to force: a real directory
    tree, with real ``.blue``/``.green`` slots, that is emphatically not
    ``~/.coord-venv``.

    Returns the venv dir to pass as ``venv_dir``, and exports it as
    ``COORD_VENV_DIR`` so code that resolves the path itself (the
    ``/update`` and ``/rollback`` endpoints) lands here too. Work that
    needs to exercise upgrade/restart behaviour end to end targets this,
    never the daemon host's own runtime.
    """
    root = tmp_path / "sacrificial"
    venv_dir = root / ".coord-venv"
    venv_dir.mkdir(parents=True)
    monkeypatch.setenv("COORD_VENV_DIR", str(venv_dir))
    return venv_dir


@pytest.fixture(autouse=True)
def _no_real_pause_store(monkeypatch, tmp_path):
    """#2101: never let a test write the OPERATOR'S real
    ``~/.coord/paused_machines.json``.

    Exactly the ``_no_real_agent_venv`` hazard one file over, and it bit the
    same way during #2101's development: `coord release propagate` grew a step
    that cordons every host behind the release, `tests/test_cli_release_
    propagate.py` drives that command for real, and nothing in that module
    isolates ``$HOME`` — so one pytest run left two release cordons in the dev
    machine's live pause store. Every subsequent test that asked
    ``paused_set()`` for a dispatch target then got "everything is paused",
    which is why ~290 tests across `test_review`/`test_test_author`/… failed
    with nothing in their own diffs to explain it.

    A machine-state file that a test can write is a machine-state file a test
    WILL write. This redirects the store to a per-test tmp file whenever
    ``$HOME`` is *still* whatever it was when this ambient value was
    snapshotted (see ``_AMBIENT_HOME_AT_COLLECTION`` above) — i.e. nobody's
    own fixture has claimed responsibility for it — and leaves it alone for
    the tests that already point ``$HOME`` at their own tmp dir (which keeps
    `test_machine_pause.py`'s and #2101's own on-disk assertions meaningful).

    #2615: this used to compare the resolved state-file path against the
    OS-level real home directory (``pwd.getpwuid(os.getuid()).pw_dir``)
    instead. That is indistinguishable from "no redirect needed" on a
    developer's own machine, where the two are the same path — but under
    ``scripts/run_tests_in_populated_home.sh`` (#2170), ``$HOME`` is a
    throwaway thin-client directory shared by the WHOLE pytest session, not
    the real pwd home, so the old check never fired: the store silently
    became session state, and a real (unmocked)
    ``machine_pause.set_cordon("laptop", ...)`` call anywhere in the suite
    (``tests/test_drive_queue_roll_pending.py``, added by #2607) poisoned
    every test that ran after it in the same session — 70 failures across
    unrelated modules (`test_model_tiering`, `test_plan_only`,
    `test_reconcile`, `test_test_author`, `test_milestone_dispatch`, …), each
    asking "is `laptop` paused?" and getting a stale "yes" from a cordon some
    earlier, unrelated test wrote for real.

    Comparing the *current* ``$HOME`` env var against the collection-time
    snapshot (rather than checking whether the resolved path descends from
    it) matters too: `run_tests_in_populated_home.sh` also nests `$TMPDIR`
    (and so every fixture's `tmp_path`) *inside* that same throwaway
    ``$HOME`` (#2170's own knob 3). A "does the resolved path descend from
    the ambient home" check would then also catch — and wrongly override —
    a test's own ``tmp_home``-style fixture, since that fixture's `tmp_path`-
    based `$HOME` is itself a descendant of the ambient one in that mode.
    Exact equality of ``$HOME`` sidesteps that: once a test has pointed
    ``$HOME`` anywhere else, this backs off unconditionally, wherever that
    elsewhere is.

    #2776: the ``$HOME``-equality check above is POSIX-only logic wearing a
    platform-neutral name. ``default_coord_dir()`` (``coord/platform_paths.
    py``) resolves the Windows state root through ``platformdirs`` ->
    ``SHGetFolderPathW``, which reads the real shell folder and honours no
    environment variable at all — not ``HOME``, not ``USERPROFILE``. On
    ``windows-latest`` ``$HOME`` is normally unset, so ``current_home is not
    None`` is false, the branch above never fires, and every test in the
    session shares one real, machine-global ``paused_machines.json`` — the
    exact #2101 hazard this fixture exists to prevent, just unguarded on
    that platform. The 20 modules that isolate via
    ``monkeypatch.setenv("HOME", tmp_path)`` don't save it either: that env
    var redirects nothing on Windows, so ``machine_pause._state_path()``
    still resolves to the one shared file regardless.

    The first cut of this fix branched on ``sys.platform == "win32"``
    directly. That is correct on a *real* Windows runner, but it made the
    guard untestable pre-Windows-CI: the issue's own local reproduction (a
    pytest plugin — ``winsim.py`` — that fakes ``default_coord_dir()`` to
    always return one fixed global root, mirroring what ``platformdirs``
    does on Windows, *without* touching ``sys.platform``) runs with
    ``sys.platform`` still ``"linux"`` throughout. (Flipping ``sys.platform``
    for real in that repro doesn't work either — it breaks unrelated lazy,
    ``msvcrt``-guarded imports elsewhere, e.g. ``click._winconsole``, that
    only resolve correctly while genuinely on Linux, so any module
    importing ``click`` after the flip fails to collect at all.) A branch
    keyed on ``sys.platform`` therefore never engaged under that repro, and
    it kept reproducing the original failures after the "fix" too.

    Trusting an observed *behaviour* instead of the platform string fixes
    that: probe whether ``$HOME`` is actually load-bearing for this
    resolution by re-resolving under a different ``$HOME`` value and
    comparing. On POSIX — real, or under this repro's unfaked
    ``default_coord_dir()`` — the two resolutions differ, so nothing
    changes here: the ``$HOME``-equality check above still governs, #2615
    and #2170 intact. On real ``win32``, and identically under this
    repro's faked ``default_coord_dir()`` (which ignores ``$HOME`` on
    purpose, exactly like ``platformdirs`` does), the probe comes back
    identical, so this redirects unconditionally — regardless of what
    ``sys.platform`` happens to read — which is what lets the *exact*,
    unmodified reproduction from the issue demonstrate the fix locally.

    One more wrinkle the probe alone doesn't cover: some tests (e.g.
    ``tests/test_quiet_hours_store_2146.py``) reach past the public API and
    read the on-disk file back at a hardcoded ``$HOME / ".coord" /
    "paused_machines.json"`` path, to assert on the raw JSON. Those tests
    already point ``$HOME`` at their own private ``tmp_path`` (the
    20-module isolation pattern), so when the probe says ``$HOME`` isn't
    load-bearing *and* a test has pointed it somewhere private (not the
    ambient snapshot), this mirrors the POSIX ``~/.coord`` layout under
    that private directory instead of the fixture's own unrelated sandbox
    — same isolation guarantee, but at the path such a test already
    expects. When no test has claimed ``$HOME`` at all (the common
    ``windows-latest`` case: unset, or still the ambient snapshot), there
    is nothing to mirror under, so this falls back to the fixture's own
    private sandbox, exactly as before.

    A test that has taken explicit control via the ``$COORD_DIR`` override
    this issue adds (``coord/platform_paths.py``) is trusted outright and
    skips all of the above — the same way the sibling ``_no_real_*``
    fixtures below trust their own env-var seams.
    """
    from coord import machine_pause  # noqa: PLC0415

    original = machine_pause._state_path
    sandbox = tmp_path / "pause-store"

    def _sandboxed(name: str) -> Path:
        sandbox.mkdir(parents=True, exist_ok=True)
        return sandbox / name

    def _guarded() -> Path:
        resolved = original()

        if os.environ.get("COORD_DIR"):
            # A test has taken explicit control via the documented
            # override seam (#2776) — trust it, don't second-guess it.
            return resolved

        current_home = os.environ.get("HOME")
        is_ambient_home = False
        try:
            is_ambient_home = (
                current_home is not None
                and Path(current_home).resolve() == _AMBIENT_HOME_AT_COLLECTION
            )
        except OSError:
            pass

        # Probe: does changing $HOME actually change what this resolves
        # to? If not, there is no $HOME-shaped signal here worth trusting
        # (real win32, or a repro faking the same env-immunity) and we
        # must redirect unconditionally.
        _unset = object()
        saved_home = os.environ.get("HOME", _unset)
        os.environ["HOME"] = f"{current_home or ''}/__coord_home_probe__"
        try:
            probed = original()
        finally:
            if saved_home is _unset:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
        home_is_load_bearing = probed != resolved

        if not home_is_load_bearing:
            if current_home is not None and not is_ambient_home:
                # A test claimed $HOME as its own private isolation dir,
                # but this platform's resolution ignores $HOME outright --
                # mirror the POSIX layout under that private dir so an
                # on-disk assertion built against it stays meaningful.
                mirror = Path(current_home) / ".coord"
                mirror.mkdir(parents=True, exist_ok=True)
                return mirror / resolved.name
            return _sandboxed(resolved.name)

        if is_ambient_home:
            return _sandboxed(resolved.name)
        return resolved

    monkeypatch.setattr(machine_pause, "_state_path", _guarded)


@pytest.fixture(autouse=True)
def _no_real_notifier_state(monkeypatch, tmp_path):
    """#1632: never let a test write the OPERATOR'S real
    ``~/.coord/notifier.json``.

    Exactly the ``_no_real_pause_store`` (#2101) hazard one file over, and
    with a nastier blast radius: the notifier's state file holds the
    "already told you" ledger, so a leaked test write would either suppress
    a real phone push (a ledger entry the operator never actually received)
    or resurrect a stale one. A state file a test *can* write is a state
    file a test *will* write.

    ``coord.notifier.store.state_path`` reads ``$COORD_NOTIFIER_STATE``
    first precisely so this redirect is a one-line env set rather than a
    monkeypatched private function.
    """
    monkeypatch.setenv("COORD_NOTIFIER_STATE", str(tmp_path / "notifier-state.json"))


@pytest.fixture(autouse=True)
def _no_real_github_backoff_store(monkeypatch, tmp_path):
    """#2809: never let a test write the OPERATOR'S real
    ``~/.coord/github_backoff.json``.

    Same hazard as ``_no_real_pause_store`` (#2101) / ``_no_real_notifier_
    state`` (#1632) / ``_no_real_roll_pending_store`` (#2587), one file
    over: this file is the shared "GitHub is rate-limiting us, back off"
    signal every `gh` call consults (``coord.github_throttle``, via
    ``coord.github_ops._gh``). A leaked test write could plant a stale
    backoff that silently stalls a real fleet's `gh` calls for up to a
    minute, or — the opposite direction — coincidentally clear one a real
    incident just recorded.

    ``coord.github_throttle._state_path`` reads
    ``$COORD_GITHUB_BACKOFF_STATE`` first for exactly this redirect, the
    same env-var seam ``_no_real_notifier_state``/``_no_real_roll_pending_
    store`` use rather than a monkeypatched private function.
    """
    monkeypatch.setenv(
        "COORD_GITHUB_BACKOFF_STATE", str(tmp_path / "github-backoff-state.json")
    )


@pytest.fixture(autouse=True)
def _no_real_issues_sync_status_store(monkeypatch, tmp_path):
    """#2858: never let a test write the OPERATOR'S real
    ``~/.coord/issues_sync_status.json``.

    Same hazard as ``_no_real_github_backoff_store`` immediately above, one
    file over: this one is the per-repo issues-sync staleness clock
    (``coord.issues_sync_status``) that both ``coord.serve_app.
    _sync_issues_tick``'s starvation-floor bypass and the
    ``issues_sync_staleness`` health check read. A leaked test write could
    plant a fake stale/fresh timestamp that either spuriously trips a real
    fleet's staleness alarm or masks a real one.

    ``coord.issues_sync_status._state_path`` reads
    ``$COORD_ISSUES_SYNC_STATE`` first for exactly this redirect, the same
    env-var seam ``_no_real_github_backoff_store`` uses, not a monkeypatched
    private function.
    """
    monkeypatch.setenv(
        "COORD_ISSUES_SYNC_STATE", str(tmp_path / "issues-sync-status.json")
    )


@pytest.fixture(autouse=True)
def _no_real_repo_dormancy_store(monkeypatch, tmp_path):
    """#2994: never let a test write the OPERATOR'S real
    ``~/.coord/repo_dormancy_status.json``.

    Same hazard as ``_no_real_issues_sync_status_store`` immediately above,
    one file over: this one is the per-repo dormant-sweep floor timer
    (``coord.repo_dormancy``) that both ``coord.serve_app._sync_issues_tick``
    and ``coord.reconcile.close_stale_prs`` consult before spending a real
    ``gh`` call on a repo. A leaked test write could plant a fake recent
    sweep timestamp that spuriously skips a real fleet's dormant repo past
    its floor, or clear one and force an extra sweep early.

    ``coord.repo_dormancy._state_path`` reads
    ``$COORD_REPO_DORMANCY_STATE`` first for exactly this redirect, the same
    env-var seam ``_no_real_issues_sync_status_store`` uses, not a
    monkeypatched private function.
    """
    monkeypatch.setenv(
        "COORD_REPO_DORMANCY_STATE", str(tmp_path / "repo-dormancy-status.json")
    )


@pytest.fixture(autouse=True)
def _no_real_roll_pending_store(monkeypatch, tmp_path):
    """#2587: never let a test write the OPERATOR'S real
    ``~/.coord/roll_pending.json``.

    Same hazard as ``_no_real_pause_store`` (#2101) and
    ``_no_real_notifier_state`` (#1632), one file over — this one gates
    whether the daemon host's drive-queue tick launches anything at all, so a
    leaked test write would either wedge a real fleet's queue (a marker that
    was never supposed to exist) or mask one (clearing/overwriting a real
    pending roll).
    ``coord.commands.drive_queue.roll_pending_path`` reads
    ``$COORD_ROLL_PENDING_STATE`` first for exactly this redirect — the same
    env-var seam ``_no_real_notifier_state`` uses, not a monkeypatched
    private function.
    """
    monkeypatch.setenv(
        "COORD_ROLL_PENDING_STATE", str(tmp_path / "roll-pending-state.json")
    )


@pytest.fixture(autouse=True)
def _no_real_roll_pending_ledger_store(monkeypatch, tmp_path):
    """#2889: never let a test write the OPERATOR'S real
    ``~/.coord/roll_pending_ledger.json``.

    Same hazard as ``_no_real_roll_pending_store`` immediately above, one
    file over — this one is what a fresh `RollPending` arm checks BEFORE
    writing (`coord.commands.release._ensure_roll_pending_marker`/
    `release_nightly_window`'s own arm site), so a leaked test write would
    either fabricate a real cumulative-frozen-time/rate-limit history out of
    nothing (spuriously refusing a real fleet's next arm) or mask one
    (silently clearing an operator's real escalated ledger).
    ``coord.commands.drive_queue.roll_pending_ledger_path`` reads
    ``$COORD_ROLL_PENDING_LEDGER_STATE`` first for exactly this redirect.
    """
    monkeypatch.setenv(
        "COORD_ROLL_PENDING_LEDGER_STATE", str(tmp_path / "roll-pending-ledger-state.json")
    )


@pytest.fixture(autouse=True)
def _no_real_self_cordon_state(monkeypatch, tmp_path):
    """#2572: never let a test write the OPERATOR'S real
    ``~/.coord/self_cordon_escalation.json``.

    Same hazard as ``_no_real_roll_pending_store`` immediately above, one
    file over: this marker tracks how long a self-cordon has persisted, and
    a leaked test write could either fabricate a 30-minute-old self-cordon
    out of nothing (spuriously firing the direct ntfy escalation the first
    time a test's clock crosses the threshold) or mask a real one (clearing
    it out from under an operator's actual stuck fleet).
    ``coord.commands.drive_queue._self_cordon_state_path`` reads
    ``$COORD_SELF_CORDON_STATE`` first for exactly this redirect.
    """
    monkeypatch.setenv(
        "COORD_SELF_CORDON_STATE", str(tmp_path / "self-cordon-state.json")
    )


@pytest.fixture(autouse=True)
def _no_dispatch_target_validation(monkeypatch):
    """#2087: default the dispatch-target gate (`record_dispatched` /
    `record_dispatched_assignment` refusing an assignment whose repo/machine
    isn't in `coordinator.yml`) to a no-op in tests.

    Without this, every test in this suite that calls those functions (or the
    `_local` writers directly) would depend on whatever REAL
    `~/.coord/coordinator.yml` happens to exist on the machine running
    pytest, instead of the fixture repo/machine names (`api`/`shared`/
    `laptop`/`server`, plus dozens of test-local ad hoc names like
    `dellserver`/`precision`/`m1`) the suite actually uses — exactly the
    non-hermetic coupling `_no_board_service` / `_no_real_agent_venv` above
    already exist to prevent. This is the same incident mechanism #2087
    itself reports: a scratch script hitting the default, unmocked config
    path landing on whatever real config happens to be there.

    Production never monkeypatches this — `coord.state._dispatch_target_config`
    always loads the real `coordinator.yml`. Tests exercising the gate itself
    (`tests/test_dispatch_target_validation.py`) monkeypatch this seam back to
    a real or fixture `Config`, which — since monkeypatch application order
    means a test's own `monkeypatch.setattr` call always runs after (and so
    wins over) this autouse fixture's — overrides the no-op default.
    """
    monkeypatch.setattr("coord.state._dispatch_target_config", lambda: None)


@pytest.fixture(autouse=True)
def coord_db():
    """Isolated database on the active backend, active for every test automatically.

    Overrides the module-level singleton in coord.db so that all state
    functions (save_board, load_board, record_dispatched, etc.) operate on a
    fresh, private database rather than the real ``~/.coord/coord.db``.

    autouse=True means no test needs to request this fixture explicitly —
    every test gets a clean DB and can never leak rows into the real database.
    Tests that need the connection object (e.g. to inspect raw rows) can still
    declare ``coord_db`` in their parameter list to receive it.

    #2884: *which* backend that is comes from ``COORD_TEST_BACKEND``
    (``sqlite`` by default, ``postgres`` opt-in) via
    :func:`tests.backends.open_session`.  This is the whole suite's backend
    switch — because this fixture is autouse and routes through
    ``db.override_connection()``, all 393 test files follow it for free.

    Deliberately **not** a ``pytest.fixture(params=...)``: parametrising here
    would double the runtime of the entire suite for every developer, on
    every run, forever.  The env var keeps the default path byte-identical to
    the pre-#2884 behaviour (``sqlite3.connect(":memory:")`` +
    ``sqlite3.Row``) and makes the second backend something CI opts into.

    The schema still comes from ``coord.db._ensure_schema()`` on whatever
    connection the harness opened — it infers its dialect from the connection
    object (#2724), so this fixture is the first consumer of #827's
    "does Postgres get the same migration path" decision rather than a
    second, divergent copy of the schema.
    """
    from coord import db
    from coord.db import _ensure_schema
    from tests.backends import open_session

    session = open_session()
    _ensure_schema(session.conn)
    db.override_connection(session.conn)
    try:
        yield session.conn
    finally:
        # session.close() first: on Postgres it has to DROP the private
        # schema, which needs a usable connection. db.close() afterwards
        # resets coord.db's singleton back to None (and closing an
        # already-closed connection is a no-op on both drivers).
        session.close()
        db.close()


# Every module-attribute name that coord.db / coord.state / coord.config /
# coord.agent resolve lazily via PEP 562 ``__getattr__`` (#2781) -- see
# ``_no_frozen_coord_dir_constants`` below for why this list has to exist at
# all.
_LAZY_COORD_DIR_CONSTANTS = {
    "coord.db": ("COORD_DIR", "DB_PATH"),
    "coord.state": (
        "COORD_DIR",
        "PROPOSALS_FILE",
        "SPLITS_FILE",
        "DISPATCHED_FILE",
        "NOTIFIED_FILE",
        "BOARD_FILE",
        "SESSION_FILE",
        "PLANS_FILE",
    ),
    "coord.config": ("USER_CONFIG_PATH",),
    "coord.agent": ("DEFAULT_STATE_DIR",),
}


def _scrub_lazy_coord_dir_constants() -> None:
    import coord.agent as agent_mod  # noqa: PLC0415
    import coord.config as config_mod  # noqa: PLC0415
    import coord.db as db_mod  # noqa: PLC0415
    import coord.state as state_mod  # noqa: PLC0415

    modules = {
        "coord.db": db_mod,
        "coord.state": state_mod,
        "coord.config": config_mod,
        "coord.agent": agent_mod,
    }
    for module_name, names in _LAZY_COORD_DIR_CONSTANTS.items():
        module = modules[module_name]
        for name in names:
            module.__dict__.pop(name, None)


@pytest.fixture(autouse=True)
def _no_frozen_coord_dir_constants():
    """Undo ``monkeypatch``/``mock.patch`` freezing the #2781 lazy constants.

    ``coord.db.COORD_DIR``/``DB_PATH``, ``coord.state.COORD_DIR`` (+ its
    legacy file-path constants), ``coord.config.USER_CONFIG_PATH`` and
    ``coord.agent.DEFAULT_STATE_DIR`` are all resolved lazily via a
    module-level ``__getattr__`` (#2781) specifically so that ``$COORD_DIR``
    set *after* a module is first imported -- e.g. by a pytest fixture --
    still reaches them, matching :func:`coord.platform_paths.default_coord_dir`
    itself.

    But both ``pytest``'s ``monkeypatch.setattr`` and ``unittest.mock.patch``
    save the "original" value by calling ``getattr(module, name, notset)``
    before patching. Because ``__getattr__`` never raises ``AttributeError``
    for these known names, that call always succeeds with a freshly computed
    real ``Path`` -- never the "attribute didn't exist" sentinel both
    libraries rely on. On teardown they therefore call
    ``setattr(module, name, <that captured Path>)`` (not ``delattr``), which
    permanently binds the name into the module's ``__dict__``. From that
    point on, plain attribute access on that name never reaches
    ``__getattr__`` again for the rest of the process -- silently reverting
    to the pre-#2781 "frozen at first monkeypatch" behaviour for every test
    that runs afterwards, defeating the entire point of #2781 for the rest
    of the session. ``tests/test_db.py`` (``COORD_DIR``/``DB_PATH``),
    ``tests/test_config.py`` (``USER_CONFIG_PATH``) and several others all do
    exactly this, and pytest collects files alphabetically by default, so a
    full-suite run reliably poisons all four modules long before
    ``tests/test_platform_paths.py``'s own post-import-redirect tests run.

    Fixing this by migrating every existing ``monkeypatch.setattr``/
    ``mock.patch`` call site (dozens, across many files with no lighter
    touch available -- see #2781's review) would be a huge, unrelated blast
    radius. Scrubbing these specific names back out of each module's
    ``__dict__`` is far more surgical: it forces ``__getattr__`` to engage
    again on the very next access, regardless of which earlier test (or
    which of the two mechanisms) did the poisoning.

    Scrubbing on *setup*, before the test body runs, is what actually makes
    this reliable -- not scrubbing on teardown. The first version of this
    fixture only scrubbed in teardown, on the assumption that autouse
    function-scoped fixtures tear down *after* explicitly requested ones of
    the same scope (true in isolation -- verified separately). But this repo
    already has an earlier autouse fixture that itself requests
    ``monkeypatch`` as a dependency (``_no_dispatch_target_validation``
    above), and a fixture's dependencies are set up before it runs -- so
    that earlier fixture pulls the *shared* function-scoped ``monkeypatch``
    instance's setup forward to before this fixture even runs, which pushes
    ``monkeypatch``'s own finalizer (the thing that does the re-poisoning
    ``setattr``) to teardown *after* this fixture's teardown, not before.
    Relying on intra-test teardown ordering to race a fixture already
    embedded elsewhere in this same conftest turned out to be exactly the
    kind of fragile assumption this fixture exists to route around. Scrubbing
    on setup instead sidesteps the race entirely: pytest always finishes a
    test's *entire* teardown phase (whatever internal order it happens in,
    including ``monkeypatch.undo()``) before the next test's setup phase
    begins, so scrubbing here guarantees a clean slate for every test
    regardless of what the previous test's fixtures did or in what order.
    Teardown here too, defensively, in case anything reads these constants
    between this test's body and the next test's setup (e.g. another
    fixture's own teardown).
    """
    _scrub_lazy_coord_dir_constants()
    yield
    _scrub_lazy_coord_dir_constants()
