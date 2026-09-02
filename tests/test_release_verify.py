"""`coord release verify` and the `spawned_coord` lane behind it (#1834).

The centrepiece is
:func:`test_2026_08_04_daemon_spawns_two_releases_back_is_caught`, which
replays the exact drift the command was written for: a daemon host whose
agent venv, CLI venv, coord-serve process, unit files and PyPI index all read
0.4.105 while ``shutil.which("coord")`` inside the running ``coord-serve``
resolved to an editable checkout on 0.4.103. Four green readouts, one split
brain. Per #1544's standard — *a check that has never caught the bug it was
written for is not a check* — that test drives the real probe against a real
(temporary) console script and a real fake ``/proc`` entry, not a hand-shaped
result dict, so it fails if any link in the chain regresses:

    live PATH -> shutil.which -> the resolved binary -> its version
        -> the machine-scope CheckResult -> the fleet lane map
        -> `coord release verify`'s exit code

Everything else here pins the design constraints the issue spells out:
skew-between-lanes rather than staleness-within-one, editable-is-a-finding-on-
its-own, unreachable-is-never-OK, and read-only.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import __version__ as OWN_VERSION
from coord import release_verify as rv
from coord.config import HealthConfig, _DEFAULT_SPAWNED_COORD_UNITS
from coord.health.checks import spawned_coord
from coord.health.models import FleetSnapshot, HealthContext, Severity
from coord.health.registry import run_all

NOW = 1_800_000_000.0

RELEASED = "0.4.105"
STALE = "0.4.103"


# ──────────────────────────────────────────────────────────────────────────
# helpers: build the payload shapes the real transports produce
# ──────────────────────────────────────────────────────────────────────────


def _result(check_id: str, *, subject: str | None = None, severity: str = "ok",
            headroom: str = "", detail: str = "", error: str | None = None,
            **values) -> dict:
    """One row of an agent's `/health` -> `health.results` list."""
    row = {
        "key": f"{check_id}:{subject}" if subject else check_id,
        "check_id": check_id,
        "scope": "machine",
        "subject": subject,
        "severity": severity,
        "headroom": headroom,
        "detail": detail,
        "values": values,
    }
    if error:
        row["error"] = error
    return row


def _health(*results: dict, version: str | None = RELEASED) -> dict:
    """An agent `/health` body, shaped like coord/agent.py's.

    ``version`` defaults to :data:`RELEASED` and feeds `lanes_for_host`'s
    ``coord-agent process`` lane (#2841) — the agent's own frozen-at-start
    version, distinct from whatever `agent_venv`/`spawned_coord` rows a test
    also passes in. A test simulating a genuinely uniform fleet (every lane
    agreeing on one version, stale or not) must pass a matching ``version``
    here too, or this lane alone would manufacture skew that was never the
    point of that test.
    """
    return {"version": version, "health": {"schema": 1, "results": list(results)}}


def _agent_venv(version: str | None, *, editable: bool = False) -> dict:
    return _result("agent_venv", version=version, editable=editable)


def _cli_venv(version: str | None) -> dict:
    return _result("cli_venv", present=True, version=version, editable=False)


def _spawns(unit: str, version: str | None, *, editable: bool = False,
            severity: str = "ok", resolved: str = "/x/bin/coord") -> dict:
    return _result(
        "spawned_coord", subject=unit, severity=severity,
        unit=unit, pid=42, version=version, editable=editable,
        resolved=resolved, fallback=False,
    )


# ──────────────────────────────────────────────────────────────────────────
# the reproduction: 2026-08-04, end to end through the real probe
# ──────────────────────────────────────────────────────────────────────────


def _fake_coord_script(tmp_path: Path, *, version: str, editable: bool) -> Path:
    """A real `coord` console script whose interpreter reports *version*.

    Deliberately a genuine executable with a genuine shebang rather than a
    monkeypatched lookup: the thing under test is that ``shutil.which`` on a
    live PATH finds *this* file and that we can read a version out of it, and
    a stubbed resolver would assert nothing about either.

    *editable* controls whether the interpreter's ``coord.__file__`` lands
    under ``site-packages`` (a release) or in a bare checkout directory (an
    editable install) — the exact signal
    :func:`coord.health.checks.spawned_coord.is_editable` reads.
    """
    root = tmp_path / ("checkout" if editable else "release")
    if editable:
        module_file = root / "src" / "claude-coordinator" / "coord" / "__init__.py"
    else:
        module_file = (
            root / "lib" / "python3" / "site-packages" / "coord" / "__init__.py"
        )
    module_file.parent.mkdir(parents=True, exist_ok=True)
    module_file.write_text(f'__version__ = "{version}"\n')

    # A stand-in interpreter: `python -c "import coord;..."` is what the probe
    # runs, so a shim that answers with this version + module path is a
    # faithful substitute for a whole second venv, and is fast enough to run
    # in a unit test.
    interpreter = root / "bin" / "python3"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n%s\\n" "{version}" "{module_file}"\n'
    )
    interpreter.chmod(interpreter.stat().st_mode | stat.S_IEXEC)

    bindir = root / "bin"
    script = bindir / "coord"
    script.write_text(f"#!{interpreter}\n# console script stub\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _fake_proc(tmp_path: Path, pid: int, path_value: str) -> Path:
    """A `/proc/<pid>/environ` that holds *path_value*, NUL-separated."""
    proc_root = tmp_path / "proc"
    entry = proc_root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "environ").write_bytes(
        b"LANG=C\0PATH=" + path_value.encode() + b"\0HOME=/home/x\0"
    )
    return proc_root


def _ctx(home: Path) -> HealthContext:
    return HealthContext(
        thresholds=HealthConfig(),
        home=home,
        coord_dir=home / ".coord",
        now=NOW,
        allow_network=False,
    )


def test_2026_08_04_daemon_spawns_two_releases_back_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident, replayed through the real probe and the real command.

    dellserver's `coord-serve` unit began its `Environment=PATH=` with an
    editable checkout of claude-coordinator two releases behind. The daemon
    process itself was 0.4.105; every subprocess `coord_argv()` spawned was
    0.4.103. PyPI, `coord status` on all three agents,
    `~/.coord-venv/bin/coord version` and `~/.local/bin/coord version` all
    said 0.4.105.
    """
    stale = _fake_coord_script(tmp_path, version=STALE, editable=True)
    released = _fake_coord_script(tmp_path, version=RELEASED, editable=False)

    # The unit's PATH, verbatim in shape: the editable checkout FIRST, the
    # release entry point after it. This is the whole bug.
    service_path = f"{stale.parent}:{released.parent}:/usr/bin:/bin"
    proc_root = _fake_proc(tmp_path, 4242, service_path)

    monkeypatch.setattr(spawned_coord, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(
        spawned_coord, "running_unit_pids", lambda units: {"coord-serve": 4242}
    )
    # The reporting process (the agent on that host) is on the release.
    monkeypatch.setattr(spawned_coord, "OWN_VERSION", RELEASED)

    # ── 1. the machine-scope probe sees it ───────────────────────────────
    results = spawned_coord.probe_spawned_coord(_ctx(tmp_path))
    assert len(results) == 1
    row = results[0]
    assert row.subject == "coord-serve"
    assert row.severity is Severity.CRIT, row.headroom
    assert row.values["version"] == STALE
    assert row.values["resolved"] == str(stale)
    assert row.values["editable"] is True

    # ── 2. shutil.which really is what picked the stale one ──────────────
    assert spawned_coord.resolve_coord(service_path) == str(stale)

    # ── 3. `coord release verify` fails, naming the host AND the lane ────
    health = _health(
        _agent_venv(RELEASED),
        _cli_venv(RELEASED),
        _result("unit_drift", subject="coord-serve.service", severity="ok",
                matches=True),
        _spawns("coord-serve", STALE, editable=True, severity="crit",
                resolved=str(stale)),
    )
    report = rv.verify(
        machine_health={"dellserver": health},
        daemon_host={"coord_serve_version": RELEASED},
        expected=RELEASED,
    )
    assert report.severity == "crit"
    assert report.exit_code == rv.EXIT_CRIT

    rendered = rv.render(report)
    assert "dellserver" in rendered
    assert "coord-serve spawns" in rendered
    assert STALE in rendered
    # The four readouts that lied must still be shown as green lanes — the
    # report's value is the *relationship*, and hiding the agreeing lanes
    # would make the skew unexplainable.
    assert RELEASED in rendered
    assert "SKEW" in rendered


def test_the_pre_1834_lane_set_alone_would_have_passed(tmp_path: Path) -> None:
    """Control for the test above: without the `spawns` lane, 2026-08-04 is
    invisible. This is why the issue calls the enumeration the deliverable.

    If someone deletes the spawned_coord projection from `lanes_for_host`,
    the reproduction test above still has to fail — this test is what proves
    the reproduction is not passing for some incidental reason.
    """
    health = _health(
        _agent_venv(RELEASED),
        _cli_venv(RELEASED),
        _result("unit_drift", subject="coord-serve.service", severity="ok"),
    )
    report = rv.verify(
        machine_health={"dellserver": health},
        daemon_host={"coord_serve_version": RELEASED},
        expected=RELEASED,
    )
    assert report.ok, report.findings


# ──────────────────────────────────────────────────────────────────────────
# spawned_coord: the machine-scope probe's own contract
# ──────────────────────────────────────────────────────────────────────────


def test_no_running_coord_service_is_ok_not_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thin clients and plain workers run no coord user units at all. That is
    an absence, not a fault — same convention as cli_venv/tui_binary."""
    monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda units: {})
    (result,) = spawned_coord.probe_spawned_coord(_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.subject is None
    assert "no coord service running" in result.headroom


def test_matching_spawned_version_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    released = _fake_coord_script(tmp_path, version=RELEASED, editable=False)
    proc_root = _fake_proc(tmp_path, 7, f"{released.parent}:/usr/bin")
    monkeypatch.setattr(spawned_coord, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda u: {"coord-agent": 7})
    monkeypatch.setattr(spawned_coord, "OWN_VERSION", RELEASED)

    (result,) = spawned_coord.probe_spawned_coord(_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.values["version"] == RELEASED
    assert result.values["editable"] is False


def test_editable_is_crit_even_when_the_version_agrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1834, explicitly: "any editable install on a service PATH is a finding
    on its own, independent of its current version — it is a drift amplifier
    that silently tracks a checkout nothing keeps current."."""
    editable = _fake_coord_script(tmp_path, version=RELEASED, editable=True)
    proc_root = _fake_proc(tmp_path, 8, f"{editable.parent}:/usr/bin")
    monkeypatch.setattr(spawned_coord, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda u: {"coord-serve": 8})
    monkeypatch.setattr(spawned_coord, "OWN_VERSION", RELEASED)

    (result,) = spawned_coord.probe_spawned_coord(_ctx(tmp_path))
    assert result.severity is Severity.CRIT
    assert result.values["version"] == RELEASED  # agrees...
    assert "EDITABLE" in result.headroom       # ...and is still a finding


def test_no_coord_on_the_service_path_is_ok_not_a_missing_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`coord_argv()` falls back to `sys.executable -m coord.cli` — the
    parent's own install, which cannot disagree with the parent. Reporting
    that as a gap would put a permanent UNKNOWN on every correct fleet."""
    proc_root = _fake_proc(tmp_path, 9, "/nonexistent-a:/nonexistent-b")
    monkeypatch.setattr(spawned_coord, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda u: {"coord-web": 9})

    (result,) = spawned_coord.probe_spawned_coord(_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.values["fallback"] is True
    assert result.values["version"] is None


def test_unreadable_process_environ_is_unknown_never_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service running as another user is unverified, which is not the same
    as in sync."""
    monkeypatch.setattr(spawned_coord, "_PROC_ROOT", tmp_path / "empty-proc")
    monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda u: {"coord-serve": 1})

    (result,) = spawned_coord.probe_spawned_coord(_ctx(tmp_path))
    assert result.severity is Severity.UNKNOWN
    assert result.error


def test_process_path_reads_the_kernels_copy_not_the_unit_file(
    tmp_path: Path,
) -> None:
    """The reason this check exists alongside `unit_drift`: a drop-in, an
    EnvironmentFile, or `systemctl --user set-environment` changes the live
    PATH without touching any file `unit_drift` reads."""
    proc_root = _fake_proc(tmp_path, 55, "/injected/bin:/usr/bin")
    assert (
        spawned_coord.process_path(55, proc_root=proc_root)
        == "/injected/bin:/usr/bin"
    )
    assert spawned_coord.process_path(56, proc_root=proc_root) is None


def test_is_editable_is_unknown_not_false_without_a_module_path() -> None:
    """Guessing "not editable" from missing evidence would silence exactly
    the finding this exists for."""
    assert spawned_coord.is_editable(None) is None
    assert spawned_coord.is_editable("/x/lib/python3.11/site-packages/coord/__init__.py") is False
    assert spawned_coord.is_editable("/home/j/src/claude-coordinator/coord/__init__.py") is True


def test_spawned_identity_ignores_the_caller_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2227: ``python -c "import coord"`` prepends the *subprocess's* cwd to
    ``sys.path`` before site-packages is ever consulted. With no ``cwd=`` (and
    no ``-P``) on the ``subprocess.run`` call, that subprocess inherits
    whatever directory the operator's shell happened to be in when they ran
    ``coord health`` — so running it from inside a coord checkout made this
    probe read ``./coord/__init__.py`` off the caller's cwd and report a
    healthy, non-editable venv as an editable checkout.

    Black-box per the issue: a real ``coord/`` package sitting in the
    process's cwd must not change the verdict for a real interpreter's own
    install. Uses the actual interpreter running this test (not a shim, like
    :func:`_fake_coord_script`'s ``/bin/sh`` stand-in) so a regression back to
    a bare ``[interpreter, "-c", code]`` call — with no ``-P`` — fails here.
    """
    decoy_root = tmp_path / "decoy-cwd"
    decoy_pkg = decoy_root / "coord"
    decoy_pkg.mkdir(parents=True)
    (decoy_pkg / "__init__.py").write_text('__version__ = "999.999.999"\n')

    binary = tmp_path / "coord"
    binary.write_text(f"#!{sys.executable}\n# console script stub\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    monkeypatch.chdir(decoy_root)
    version, module_file, error = spawned_coord.spawned_identity(str(binary))

    assert error is None
    assert version == OWN_VERSION
    assert module_file is not None
    assert not Path(module_file).is_relative_to(decoy_root)


def _real_coord_script(root: Path, *, editable: bool, version: str) -> tuple[Path, Path]:
    """A genuine console script whose shebang is the real interpreter
    (``sys.executable``), paired with a fake ``coord`` install reachable only
    via ``PYTHONPATH`` — not a ``/bin/sh`` shim, so ``-P`` actually has
    something to suppress.

    Returns ``(script, install_dir)``; the caller sets ``PYTHONPATH`` to
    *install_dir* so the real interpreter's ``import coord`` resolves *this*
    package rather than whatever (if anything) is actually installed for
    ``sys.executable``.
    """
    install_dir = root / ("release" if not editable else "checkout")
    site_dir = install_dir if editable else install_dir / "lib" / "site-packages"
    (site_dir / "coord").mkdir(parents=True)
    (site_dir / "coord" / "__init__.py").write_text(f'__version__ = "{version}"\n')

    script = root / "coord"
    script.write_text(f"#!{sys.executable}\n# console script stub\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, site_dir


def _probe_from_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cwd: Path,
    script: Path,
    pythonpath: Path,
    version: str,
):
    """Run the real `spawned_coord` machine-scope check with the process cwd
    set to *cwd* and a fake install pinned via `PYTHONPATH`, and return the
    single `spawned_coord` `CheckResult` it produces."""
    fake_pid = os.getpid() + 1
    proc_root = _fake_proc(tmp_path, fake_pid, f"{script.parent}:/usr/bin")
    monkeypatch.setattr(spawned_coord, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda u: {"coord-agent": fake_pid})
    monkeypatch.setattr(spawned_coord, "OWN_VERSION", version)
    monkeypatch.setenv("PYTHONPATH", str(pythonpath))
    monkeypatch.chdir(cwd)
    (result,) = spawned_coord.probe_spawned_coord(_ctx(tmp_path))
    return result


def test_full_check_ignores_the_callers_cwd_for_a_clean_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2190, end to end through the real probe: a genuinely non-editable
    install must read ``OK`` regardless of whether the operator happened to
    invoke ``coord health`` from inside a directory that itself contains a
    ``coord/`` package (e.g. a checkout of this repo). Before `-P` this
    reported CRIT — a false positive that depends entirely on the caller's
    shell cwd, not on anything the venv actually contains.
    """
    version = "7.7.7"
    script, pythonpath = _real_coord_script(tmp_path / "install", editable=False, version=version)

    plain_cwd = tmp_path / "plain-cwd"
    plain_cwd.mkdir()
    decoy_cwd = tmp_path / "decoy-cwd"
    (decoy_cwd / "coord").mkdir(parents=True)
    (decoy_cwd / "coord" / "__init__.py").write_text('__version__ = "0.0.1-decoy"\n')

    result_plain = _probe_from_cwd(
        tmp_path, monkeypatch, cwd=plain_cwd, script=script, pythonpath=pythonpath, version=version
    )
    assert result_plain.severity is Severity.OK
    assert result_plain.values["editable"] is False
    assert result_plain.values["version"] == version

    result_decoy = _probe_from_cwd(
        tmp_path, monkeypatch, cwd=decoy_cwd, script=script, pythonpath=pythonpath, version=version
    )
    assert result_decoy.severity is Severity.OK
    assert result_decoy.values["editable"] is False
    assert result_decoy.values["version"] == version

    assert result_plain.severity == result_decoy.severity
    assert result_plain.values == result_decoy.values


def test_full_check_editable_crit_survives_the_callers_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of #2190's acceptance criteria: `-P` must not weaken
    genuine #1834 detection. A real editable install reads CRIT no matter
    what the operator's shell cwd is — including a cwd with no `coord/`
    package in it at all, and one that happens to have one."""
    version = "7.7.7"
    script, pythonpath = _real_coord_script(tmp_path / "install", editable=True, version=version)

    plain_cwd = tmp_path / "plain-cwd"
    plain_cwd.mkdir()
    decoy_cwd = tmp_path / "decoy-cwd"
    (decoy_cwd / "coord").mkdir(parents=True)
    (decoy_cwd / "coord" / "__init__.py").write_text('__version__ = "0.0.1-decoy"\n')

    result_plain = _probe_from_cwd(
        tmp_path, monkeypatch, cwd=plain_cwd, script=script, pythonpath=pythonpath, version=version
    )
    assert result_plain.severity is Severity.CRIT
    assert result_plain.values["editable"] is True

    result_decoy = _probe_from_cwd(
        tmp_path, monkeypatch, cwd=decoy_cwd, script=script, pythonpath=pythonpath, version=version
    )
    assert result_decoy.severity is Severity.CRIT
    assert result_decoy.values["editable"] is True

    assert result_plain.severity == result_decoy.severity
    assert result_plain.values == result_decoy.values


def test_probe_is_registered_in_the_machine_scope_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Driven through `run_all` so an id typo or a wrong scope fails here
    rather than silently never running on any machine."""
    monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda units: {})
    from coord.health import checks  # noqa: F401

    report = run_all(_ctx(tmp_path), scopes=("machine",), only=["spawned_coord"])
    assert [r.check_id for r in report.results] == ["spawned_coord"]


def test_default_units_match_the_config_default() -> None:
    """The two lists are duplicated to keep config from importing the check
    registry; this is the pin that keeps them honest."""
    assert spawned_coord.DEFAULT_UNITS == _DEFAULT_SPAWNED_COORD_UNITS
    assert spawned_coord.configured_units(_ctx(Path("/nonexistent"))) == \
        tuple(_DEFAULT_SPAWNED_COORD_UNITS)


def test_empty_configured_units_disables_the_check(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.thresholds.spawned_coord_units = []
    assert spawned_coord.configured_units(ctx) == ()
    (result,) = spawned_coord.probe_spawned_coord(ctx)
    assert result.severity is Severity.OK


def test_running_unit_pids_survives_a_machine_with_no_systemd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS, containers, thin clients. Not a fault, not a crash."""
    def boom(*a, **k):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(spawned_coord.subprocess, "run", boom)
    assert spawned_coord.running_unit_pids(("coord-serve",)) == {}


def test_running_unit_pids_ignores_inactive_and_pidless_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Proc:
        stdout = (
            "Id=coord-serve.service\nMainPID=1234\nActiveState=active\n"
            "\n"
            "Id=coord-web.service\nMainPID=0\nActiveState=inactive\n"
            "\n"
            "Id=coord-agent.service\nMainPID=0\nActiveState=active\n"
        )

    monkeypatch.setattr(spawned_coord.subprocess, "run", lambda *a, **k: Proc())
    assert spawned_coord.running_unit_pids(("coord-serve", "coord-web", "coord-agent")) == {
        "coord-serve": 1234
    }


# ──────────────────────────────────────────────────────────────────────────
# the fleet lane map
# ──────────────────────────────────────────────────────────────────────────


def _fleet_ctx(machines: dict, daemon_host: dict | None = None) -> HealthContext:
    ctx = HealthContext(
        thresholds=HealthConfig(),
        home=Path("/nonexistent-home"),
        coord_dir=Path("/nonexistent-home/.coord"),
        now=NOW,
        allow_network=False,
    )
    ctx.fleet = FleetSnapshot(machines=machines, daemon_host=daemon_host or {})
    return ctx


def _machine(*results: dict) -> dict:
    return {"state": "online", "checks": {"results": list(results)}}


def test_fleet_deploy_lanes_reads_every_unit_not_just_the_first(monkeypatch) -> None:
    """`spawned_coord` reports one row PER UNIT. A first-match read would see
    only `coord-agent` and structurally miss `coord-serve` — the one unit
    whose spawned version was the entire 2026-08-04 incident."""
    from coord.health import checks  # noqa: F401

    ctx = _fleet_ctx(
        {
            "dellserver": _machine(
                _agent_venv(RELEASED),
                _spawns("coord-agent", RELEASED),
                _spawns("coord-serve", STALE),
            )
        },
        {"coord_serve_version": RELEASED},
    )
    result = {r.check_id: r for r in run_all(ctx, scopes=("fleet",)).results}[
        "fleet_deploy_lanes"
    ]
    assert result.severity is Severity.CRIT
    assert "coord-serve spawns (dellserver)" in result.values["lanes"]
    assert result.values["lanes"]["coord-serve spawns (dellserver)"] == STALE
    assert "coord-serve spawns (dellserver)" in result.detail


def test_fleet_deploy_lanes_spawn_lane_never_manufactures_a_missing_lane() -> None:
    """A unit whose PATH has no `coord` on it is not a lane at all; admitting
    it as a null one would put a permanent UNKNOWN on every correct fleet."""
    from coord.health import checks  # noqa: F401

    fallback = _result("spawned_coord", subject="coord-web", severity="ok",
                       unit="coord-web", fallback=True, version=None)
    ctx = _fleet_ctx(
        {"dellserver": _machine(_agent_venv(RELEASED), _cli_venv(RELEASED), fallback)},
        {"coord_serve_version": RELEASED},
    )
    result = {r.check_id: r for r in run_all(ctx, scopes=("fleet",)).results}[
        "fleet_deploy_lanes"
    ]
    assert not any("spawns" in name for name in result.values["lanes"])
    # Every other lane agrees and none is missing, so the fallback unit must
    # leave the verdict at OK rather than dragging it to UNKNOWN.
    assert result.severity is Severity.OK


def test_fleet_deploy_lanes_errored_spawn_row_is_not_a_version() -> None:
    from coord.health import checks  # noqa: F401

    errored = _result("spawned_coord", subject="coord-serve", severity="unknown",
                      unit="coord-serve", version=None, error="environ unreadable")
    errored["error"] = "environ unreadable"
    ctx = _fleet_ctx(
        {"dellserver": _machine(_agent_venv(RELEASED), errored)},
        {"coord_serve_version": RELEASED},
    )
    result = {r.check_id: r for r in run_all(ctx, scopes=("fleet",)).results}[
        "fleet_deploy_lanes"
    ]
    assert not any("spawns" in name for name in result.values["lanes"])


# ──────────────────────────────────────────────────────────────────────────
# verify(): the judgement
# ──────────────────────────────────────────────────────────────────────────


def test_a_correctly_deployed_fleet_passes() -> None:
    report = rv.verify(
        machine_health={
            "dellserver": _health(_agent_venv(RELEASED), _spawns("coord-serve", RELEASED)),
            "elitebook": _health(_agent_venv(RELEASED), _cli_venv(RELEASED)),
        },
        daemon_host={"coord_serve_version": RELEASED},
        expected=RELEASED,
    )
    assert report.ok, rv.render(report)
    assert report.exit_code == rv.EXIT_OK


def test_skew_is_crit_with_no_expected_version_at_all() -> None:
    """The 2026-08-04 shape: nobody knew what to expect, but two lanes
    disagreeing was already conclusive. Staleness-within-one-lane logic
    cannot express this."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(_agent_venv(RELEASED), _spawns("coord-serve", STALE)),
        },
        daemon_host={"coord_serve_version": RELEASED},
    )
    assert report.severity == "crit"
    skew = [f for f in report.findings if f.lane == "(version skew)"]
    assert skew and STALE in skew[0].detail and RELEASED in skew[0].detail


def test_expected_version_names_the_offending_host_and_lane() -> None:
    report = rv.verify(
        machine_health={
            "dellserver": _health(_agent_venv(RELEASED)),
            "precision": _health(_agent_venv(STALE)),
        },
        expected=RELEASED,
    )
    assert report.severity == "crit"
    bad = [f for f in report.findings if f.severity == "crit"]
    assert bad
    assert any("precision" in f.host for f in bad)
    assert any(STALE in f.summary for f in bad)


def test_unreachable_host_is_unknown_never_ok() -> None:
    """"We could not ask" must not render as "verified" — that is the whole
    thesis of the issue."""
    report = rv.verify(
        machine_health={"dellserver": _health(_agent_venv(RELEASED))},
        unreachable={"precision": "connection refused"},
        expected=RELEASED,
    )
    assert not report.ok
    assert report.exit_code == rv.EXIT_WARN
    assert any(f.host == "precision" and f.severity == "unknown" for f in report.findings)


def test_a_lane_with_no_version_is_unknown_not_agreement() -> None:
    report = rv.verify(
        machine_health={"precision": _health(_agent_venv(None))},
        expected=RELEASED,
    )
    assert report.severity == "unknown"
    assert any("no version reported" in f.summary for f in report.findings)


def test_editable_agent_venv_is_crit_on_its_own() -> None:
    report = rv.verify(
        machine_health={
            "precision": _health(_agent_venv(RELEASED, editable=True)),
        },
        expected=RELEASED,
    )
    assert report.severity == "crit"
    assert any("EDITABLE" in f.summary for f in report.findings)


def test_unit_drift_and_tui_staleness_are_folded_in() -> None:
    """Lanes 3 and 4 of the issue's enumeration ride the same report rather
    than needing a second command."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(
                _agent_venv(RELEASED),
                _result("unit_drift", subject="coord-serve.service", severity="crit",
                        headroom="PATH shadow risk", detail="reorder PATH="),
                _result("tui_binary", severity="warn", headroom="binary is 30.0h older"),
            )
        },
        expected=RELEASED,
    )
    lanes = {f.lane for f in report.findings}
    assert "unit coord-serve.service" in lanes
    assert "coord-tui" in lanes
    assert report.severity == "crit"


def test_unit_drift_against_an_unverified_reference_is_reported_not_dropped() -> None:
    """#1927: this command is the trust anchor #1835 gates on, so a match the
    machine could not vouch for has to reach the report. It rides as UNKNOWN
    — it must annotate the green, not page."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(
                _agent_venv(RELEASED),
                _result(
                    "unit_drift",
                    subject="coord-serve.service",
                    severity="unknown",
                    headroom=(
                        "matches /home/john/src/claude-coordinator/deploy, but "
                        "that reference is an unverified working copy"
                    ),
                    detail="install a release wheel on this host",
                ),
            )
        },
        expected=RELEASED,
    )
    finding = next(f for f in report.findings if f.lane == "unit coord-serve.service")
    assert finding.severity == "unknown"
    assert "unverified working copy" in finding.summary
    assert report.severity == "unknown"  # annotated, not paged


def test_masked_by_policy_unit_drift_renders_no_warn_and_no_remedy() -> None:
    """#3049: `coord-release-propagate.service` is masked ON PURPOSE (manual
    release rolls by choice) and carries an entry in
    `~/.coord/watchdog-suppress.json` — the same sentinel the fleet watchdog
    already honours. The `unit_drift` WARN this drift always produces (a
    masked unit's installed copy is a symlink to /dev/null, which always
    content-diffs) must render here as "masked by policy", not as a WARN
    carrying the `cp .../restart` remedy that would re-arm the very thing
    the masking exists to prevent."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(
                _agent_venv(RELEASED),
                _result(
                    "unit_drift",
                    subject="coord-release-propagate.service",
                    severity="warn",
                    headroom=(
                        "stale — installed 1921.1h ago, 120 line(s) differ "
                        "from packaged coord 0.5.341"
                    ),
                    detail=(
                        "cp .../coord-release-propagate.service ... && "
                        "systemctl --user daemon-reload && systemctl --user "
                        "restart coord-release-propagate"
                    ),
                    suppressed=True,
                    suppress_reason="manual release rolls by choice -- masked, not broken",
                    suppress_set="2026-08-26",
                ),
            )
        },
        expected=RELEASED,
    )
    finding = next(
        f for f in report.findings if f.lane == "unit coord-release-propagate.service"
    )
    assert finding.severity == "ok"
    assert "masked by policy" in finding.summary
    assert "manual release rolls by choice" in finding.summary
    assert "2026-08-26" in finding.summary
    assert "cp " not in finding.detail
    assert "restart" not in finding.summary
    # A suppressed unit must never be the reason the whole report pages.
    assert report.severity == "ok"

    rendered = rv.render(report)
    assert "masked by policy" in rendered
    assert "cp .../coord-release-propagate.service" not in rendered


def test_masked_unit_with_no_sentinel_entry_still_warns_as_before() -> None:
    """#3049's own acceptance line: "A unit that is masked and has no
    sentinel entry should keep WARNing exactly as today; the sentinel is the
    signal, not the masking." — `values["suppressed"]` absent/false must not
    be treated as coverage."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(
                _agent_venv(RELEASED),
                _result(
                    "unit_drift",
                    subject="coord-release-propagate.service",
                    severity="warn",
                    headroom="stale — installed 1921.1h ago, 120 line(s) differ",
                    detail="cp deploy/coord-release-propagate.service ... && restart ...",
                    suppressed=False,
                    suppress_reason=None,
                    suppress_set=None,
                ),
            )
        },
        expected=RELEASED,
    )
    finding = next(
        f for f in report.findings if f.lane == "unit coord-release-propagate.service"
    )
    assert finding.severity == "warn"
    assert "masked by policy" not in finding.summary
    assert "stale" in finding.summary
    assert "cp " in finding.detail
    assert report.severity == "warn"


def test_webapp_bundle_staleness_is_folded_in() -> None:
    """Lane 5 of the issue's enumeration — the webapp bundle — rides the same
    report too, on staleness-vs-source terms rather than a version (see
    coord/health/checks/fleet_deploy_lanes.py's module docstring for why a
    version comparison would be meaningless here)."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(
                _agent_venv(RELEASED),
                _result("webapp_bundle", severity="warn",
                        headroom="bundle is 3.0h older than webapp/ source",
                        detail="check coord-web-dist-build.timer on: dellserver"),
            )
        },
        expected=RELEASED,
    )
    lanes = {f.lane for f in report.findings}
    assert "webapp bundle" in lanes
    assert report.severity == "warn"  # agent_venv alone is already clean here
    warn_findings = [f for f in report.findings if f.lane == "webapp bundle"]
    assert warn_findings[0].severity == "warn"
    assert "coord-web-dist-build.timer" in warn_findings[0].detail


def test_webapp_bundle_never_becomes_a_version_lane() -> None:
    """The bundle is SHA-versioned off a continuous publish timer (#1543),
    never pip-versioned — folding it into the version-skew map would
    manufacture permanent, meaningless skew against every other lane's
    semver string. It must never appear in report.lanes at all."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(
                _agent_venv(RELEASED),
                _result("webapp_bundle", severity="ok", headroom="up to date",
                        present=True, sha="abc123", dist_mtime=1.0),
            )
        },
        expected=RELEASED,
    )
    assert report.ok, rv.render(report)
    assert not any(lane.lane == "webapp bundle" for lane in report.lanes)
    assert "abc123" not in report.versions


def test_tui_binary_never_becomes_a_version_lane() -> None:
    """#2102's grading rule, pinned: `coord-tui` is graded by LOCAL build
    staleness (binary mtime vs. `tui/` source mtime — see
    coord/health/checks/deploy_lane_facts.py), never by comparing an
    installed version against `--expected`/`--pypi`.

    #2102's reason was contingent: a `tui/`-only release published no PyPI
    wheel, so PyPI's "latest" stayed on the OLD version while the GitHub
    Release and any freshly-`coord tui update`'d binary were ahead of it, and
    a version-vs-`expected` comparison would grade every such binary "ahead of
    expected" forever.

    #2898 (phase 3 of #2894) makes the reason structural: coord-tui releases
    from its OWN repo on its OWN `v*` tag line, so `expected` — the
    coordinator's channel's version — is not a number coord-tui's version is
    comparable to at all. See `test_a_fleet_with_independent_channel_versions_
    is_green` below for the fleet state that proves it.
    """
    report = rv.verify(
        machine_health={
            "dellserver": _health(
                _agent_venv(RELEASED),
                _result("tui_binary", severity="ok", headroom="up to date",
                        present=True, path="~/.local/bin/coord-tui",
                        binary_mtime=2.0, source_mtime=1.0),
            )
        },
        expected=RELEASED,
    )
    assert report.ok, rv.render(report)
    assert not any(lane.lane == "coord-tui" for lane in report.lanes)
    # The version lanes present are agent_venv's and the agent's own
    # self-reported process version (#2841) — tui_binary contributed no
    # entry to the skew map at all, not even an agreeing one.
    assert report.versions == {
        RELEASED: ["coord-agent process (dellserver)", "~/.coord-venv (dellserver)"]
    }


def test_a_fleet_with_independent_channel_versions_is_green() -> None:
    """#2898 acceptance criterion 3: `coord release verify` on a fleet running
    coord vA and coord-tui vB reports the tui lane green, not "behind".

    Post-split those two numbers are drawn from two repos' tag lines and move
    independently, so this is the NORMAL steady state — a coordinator on
    0.5.x next to a coord-tui on 0.2.7. If coord-tui ever entered the version
    skew map, this fleet would read as ~29 releases behind on every host,
    permanently, and `coord release propagate --rollback-on-red` gates on this
    report — it would revert good rolls forever, which is the #2052 shape this
    module exists to keep closed.
    """
    report = rv.verify(
        machine_health={
            "dellserver": _health(
                _agent_venv(RELEASED),  # coord's channel: on the expected version
                # coord-tui's channel: a completely unrelated version line,
                # and a binary that is NOT stale relative to its source.
                _result("tui_binary", severity="ok", headroom="up to date",
                        present=True, path="~/.local/bin/coord-tui",
                        version="0.2.7", binary_mtime=2.0, source_mtime=1.0),
            )
        },
        expected=RELEASED,
    )

    assert report.ok, rv.render(report)
    assert report.severity == "ok"
    # Nothing anywhere claims this host is behind...
    rendered = rv.render(report)
    assert "0.2.7" not in rendered, (
        "coord-tui's version leaked into the verify report; it is a different "
        "channel's tag line and cannot be graded against `expected`"
    )
    # ...and no finding mentions the tui lane at all.
    assert not any(f.lane == "coord-tui" for f in report.findings), [
        (f.lane, f.summary) for f in report.findings
    ]
    assert list(report.versions) == [RELEASED]


def test_a_stale_tui_binary_is_still_a_warn_not_a_version_finding() -> None:
    """#2898 must not silence the lane, only stop it being graded against the
    wrong number. Local build staleness is still reported — it is a real,
    actionable finding — it simply never enters the skew map."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(
                _agent_venv(RELEASED),
                _result("tui_binary", severity="warn",
                        headroom="binary is 30.0h older than tui/ source",
                        present=True, version="0.2.7"),
            )
        },
        expected=RELEASED,
    )
    tui = [f for f in report.findings if f.lane == "coord-tui"]
    assert len(tui) == 1, [(f.lane, f.summary) for f in report.findings]
    assert tui[0].severity == "warn"
    assert list(report.versions) == [RELEASED]


def test_absent_cli_venv_is_not_a_lane() -> None:
    """Most machines never had one. An absent optional lane must not become a
    permanent UNKNOWN."""
    absent = _result("cli_venv", present=False, version=None)
    report = rv.verify(
        machine_health={"precision": _health(_agent_venv(RELEASED), absent)},
        expected=RELEASED,
    )
    assert report.ok, rv.render(report)
    assert not any(lane.lane == "~/.coord-cli-venv" for lane in report.lanes)


def test_an_empty_fleet_reading_is_unknown_not_a_pass() -> None:
    report = rv.verify(machine_health={"precision": None}, expected=RELEASED)
    assert report.severity == "unknown"
    assert not report.ok


def test_report_renders_every_lane_even_on_success() -> None:
    """The failure mode this command exists for is a readout that says "fine"
    while hiding the lane it never looked at, so the inspected lane set is
    part of the answer, not debug output."""
    report = rv.verify(
        machine_health={"dellserver": _health(_agent_venv(RELEASED),
                                              _spawns("coord-serve", RELEASED))},
        expected=RELEASED,
    )
    out = rv.render(report)
    assert "~/.coord-venv" in out
    assert "coord-serve spawns" in out
    assert "RELEASE VERIFY: OK" in out


def test_to_dict_is_json_serialisable_and_names_lanes() -> None:
    report = rv.verify(
        machine_health={"dellserver": _health(_agent_venv(STALE))},
        expected=RELEASED,
    )
    blob = json.loads(json.dumps(report.to_dict()))
    assert blob["severity"] == "crit"
    assert blob["exit_code"] == rv.EXIT_CRIT
    assert blob["lanes"][0]["host"] == "dellserver"


# ──────────────────────────────────────────────────────────────────────────
# transport: works from a thin client, and never writes
# ──────────────────────────────────────────────────────────────────────────


class _Machine:
    def __init__(self, name: str) -> None:
        self.name = name
        self.host = name


class _Config:
    def __init__(self, machines) -> None:
        self.machines = machines
        self.health = HealthConfig()


class _Status:
    def __init__(self, *, online: bool, health=None, reason: str = "") -> None:
        self.is_online = online
        self.health = health
        self.reason = reason
        self.state = "online" if online else "offline"


def test_gather_polls_every_machine_over_http_and_records_offline_ones() -> None:
    config = _Config([_Machine("dellserver"), _Machine("precision")])
    seen: list[str] = []

    def probe(machine, timeout=5.0):
        seen.append(machine.name)
        if machine.name == "precision":
            return _Status(online=False, reason="connection refused")
        return _Status(online=True, health=_health(_agent_venv(RELEASED)))

    health, unreachable, daemon, name = rv.gather(
        config, check_machine=probe, board_payload=lambda: {}
    )
    assert seen == ["dellserver", "precision"]
    assert set(health) == {"dellserver"}
    assert unreachable == {"precision": "connection refused"}
    assert daemon is None and name == "daemon"


def test_gather_survives_a_probe_that_raises() -> None:
    config = _Config([_Machine("dellserver")])

    def probe(machine, timeout=5.0):
        raise RuntimeError("tailscale down")

    health, unreachable, _daemon, _name = rv.gather(
        config, check_machine=probe, board_payload=lambda: {}
    )
    assert health == {}
    assert "tailscale down" in unreachable["dellserver"]


def test_gather_reads_coord_serve_version_out_of_the_board_payload() -> None:
    """The daemon's own version is process-local (#1806) — a thin client can
    only get it from `/board`'s published fleet_deploy_lanes row."""
    payload = {
        "fleet_health": {
            "fleet_checks": [
                {
                    "check_id": "fleet_deploy_lanes",
                    "values": {"lanes": {rv.DAEMON_SERVE_LANE: RELEASED}},
                }
            ]
        }
    }
    config = _Config([])
    _h, _u, daemon, _n = rv.gather(
        config, check_machine=lambda m, timeout=5.0: None, board_payload=lambda: payload
    )
    assert daemon == {"coord_serve_version": RELEASED}


def test_daemon_serve_lane_name_matches_what_the_fleet_check_publishes() -> None:
    """Pins the wire-format string across the two modules: a rename in
    fleet_deploy_lanes must break here loudly, not silently drop the lane."""
    from coord.health import checks  # noqa: F401

    ctx = _fleet_ctx({}, {"coord_serve_version": RELEASED})
    result = {r.check_id: r for r in run_all(ctx, scopes=("fleet",)).results}[
        "fleet_deploy_lanes"
    ]
    assert rv.DAEMON_SERVE_LANE in result.values["lanes"]


def test_board_fetch_falls_back_to_loopback_on_the_daemon_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the daemon host `resolve_board_service()` is None (host mode reads
    the DB directly). Without this fallback, running the command *on the
    daemon host* would silently drop the `coord-serve process` lane — exactly
    the lane #1834 exists to stop losing."""
    seen: dict = {}

    monkeypatch.setattr("coord.client.resolve_board_service", lambda *a, **k: None)
    monkeypatch.setattr("coord.serve_app.resolve_serve_token", lambda *a, **k: "tok")

    def fake_fetch(svc, *, timeout=None):
        seen["url"] = svc.url
        seen["token"] = svc.token
        seen["timeout"] = timeout
        return {}

    monkeypatch.setattr("coord.client.fetch_board_payload", fake_fetch)
    assert rv._default_board_fetch() == {}
    assert seen["url"].startswith("http://127.0.0.1:")
    assert seen["token"] == "tok"
    # NOT the per-host --timeout: /board is a multi-megabyte read and a 5s
    # budget makes a healthy daemon look unreachable (a recorded gotcha).
    assert seen["timeout"] == rv._BOARD_TIMEOUT >= 30.0


def test_a_board_that_cannot_be_read_is_no_data_not_a_crash() -> None:
    def boom():
        raise ConnectionError("board unreachable")

    config = _Config([])
    _h, _u, daemon, _n = rv.gather(
        config, check_machine=lambda m, timeout=5.0: None, board_payload=boom
    )
    assert daemon is None


def test_machine_filter_polls_only_that_machine() -> None:
    config = _Config([_Machine("dellserver"), _Machine("precision")])
    seen: list[str] = []

    def probe(machine, timeout=5.0):
        seen.append(machine.name)
        return _Status(online=True, health=_health(_agent_venv(RELEASED)))

    rv.gather(config, check_machine=probe, board_payload=lambda: {},
              machine_filter="precision")
    assert seen == ["precision"]


# ──────────────────────────────────────────────────────────────────────────
# the CLI surface
# ──────────────────────────────────────────────────────────────────────────


def test_cli_release_verify_exits_nonzero_and_names_the_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from coord.cli import main

    monkeypatch.setattr(
        "coord.commands._common._load_config",
        lambda path: _Config([_Machine("dellserver")]),
    )
    monkeypatch.setattr(
        rv, "gather",
        lambda config, **kw: (
            {"dellserver": _health(_agent_venv(RELEASED),
                                   _spawns("coord-serve", STALE))},
            {},
            {"coord_serve_version": RELEASED},
            "daemon",
        ),
    )
    result = CliRunner().invoke(
        main, ["release", "verify", "--expected", "v" + RELEASED,
               "--config", str(tmp_path / "coordinator.yml")]
    )
    assert result.exit_code == rv.EXIT_CRIT, result.output
    assert "dellserver" in result.output
    assert "coord-serve spawns" in result.output
    assert STALE in result.output


def test_cli_release_verify_passes_on_a_clean_fleet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from coord.cli import main

    monkeypatch.setattr(
        "coord.commands._common._load_config",
        lambda path: _Config([_Machine("dellserver")]),
    )
    monkeypatch.setattr(
        rv, "gather",
        lambda config, **kw: (
            {"dellserver": _health(_agent_venv(RELEASED),
                                   _spawns("coord-serve", RELEASED))},
            {}, {"coord_serve_version": RELEASED}, "daemon",
        ),
    )
    result = CliRunner().invoke(
        main, ["release", "verify", "--expected", RELEASED, "--no-pypi",
               "--config", str(tmp_path / "coordinator.yml")]
    )
    assert result.exit_code == 0, result.output
    assert "RELEASE VERIFY: OK" in result.output


def test_cli_release_verify_json_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from coord.cli import main

    monkeypatch.setattr(
        "coord.commands._common._load_config",
        lambda path: _Config([_Machine("dellserver")]),
    )
    monkeypatch.setattr(
        rv, "gather",
        lambda config, **kw: (
            {"dellserver": _health(_agent_venv(RELEASED))}, {}, None, "daemon",
        ),
    )
    result = CliRunner().invoke(
        main, ["release", "verify", "--json", "--no-exit-code", "--no-pypi",
               "--config", str(tmp_path / "coordinator.yml")]
    )
    assert result.exit_code == 0, result.output
    blob = json.loads(result.output)
    assert blob["lanes"][0]["lane"] == "~/.coord-venv"


def test_cli_release_preflight_is_still_reachable_both_ways() -> None:
    """The flat command is in every operator's muscle memory and in
    docs/AGENT_OPERATIONS.md; grouping must not break it."""
    from coord.cli import main

    for argv in (["release-preflight", "--help"], ["release", "preflight", "--help"]):
        result = CliRunner().invoke(main, argv)
        assert result.exit_code == 0, (argv, result.output)
        assert "1471" in result.output


def test_verify_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    """`coord diagnose` is a documented trap for having write side effects;
    this command must be safe to run at any time, including mid-flight."""
    before = sorted(p.name for p in tmp_path.iterdir())
    rv.verify(
        machine_health={"dellserver": _health(_agent_venv(RELEASED))},
        expected=RELEASED,
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# ──────────────────────────────────────────────────────────────────────────
# #2052 fault 3 / #2035 item 4: uniform staleness must not read as health
# ──────────────────────────────────────────────────────────────────────────


def test_a_uniformly_stale_fleet_is_not_reported_clean() -> None:
    """The demonstration, not the hypothesis. After #2052's botched
    propagation reverted the fleet, every lane agreed on 0.4.104 while `main`
    was four releases ahead — and `coord release verify` said crit=0, because
    it compares the fleet against *itself*. Agreement is not currency, and a
    skew-only run must say so rather than render a clean bill of health."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(_agent_venv(STALE), version=STALE),
            "elitebook": _health(_agent_venv(STALE), version=STALE),
        },
    )
    assert not report.ok, rv.render(report)
    assert report.severity == "unknown"  # annotated, never paged
    finding = next(f for f in report.findings if f.lane == "(expected version)")
    assert "uniformly BEHIND" in finding.summary
    assert "--pypi" in finding.detail


def test_the_no_expected_finding_never_masks_real_skew() -> None:
    """Skew is already conclusive without an expected version — the #2035
    annotation must not downgrade or duplicate it."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(_agent_venv(RELEASED)),
            "elitebook": _health(_agent_venv(STALE)),
        },
    )
    assert report.severity == "crit"
    assert not any(f.lane == "(expected version)" for f in report.findings)


def test_an_expected_version_suppresses_the_annotation() -> None:
    report = rv.verify(
        machine_health={"dellserver": _health(_agent_venv(RELEASED))},
        expected=RELEASED,
    )
    assert report.ok, rv.render(report)


def test_an_empty_lane_set_does_not_get_the_no_expected_annotation() -> None:
    """"No lanes at all" already has its own, better finding; adding "and we
    don't know what to expect" on top would be noise about nothing."""
    report = rv.verify(machine_health={})
    assert not any(f.lane == "(expected version)" for f in report.findings)


# ──────────────────────────────────────────────────────────────────────────
# #2052 fault 2: the daemon host is DERIVED, never guessed
# ──────────────────────────────────────────────────────────────────────────


def test_the_daemon_host_is_derived_from_a_running_coord_serve() -> None:
    """It is not a mystery: it is the machine with a live `coord-serve`, and
    every agent already publishes exactly that in its own /health."""
    assert rv.daemon_host_from_health({
        "precision": _health(_agent_venv(RELEASED), _spawns("coord-agent", RELEASED)),
        "dellserver": _health(_agent_venv(RELEASED),
                              _spawns("coord-agent", RELEASED),
                              _spawns("coord-serve", RELEASED)),
    }) == "dellserver"


def test_no_running_coord_serve_anywhere_is_none_not_a_guess() -> None:
    assert rv.daemon_host_from_health({
        "precision": _health(_agent_venv(RELEASED), _spawns("coord-agent", RELEASED)),
    }) is None
    assert rv.daemon_host_from_health({"precision": None}) is None


def test_two_hosts_claiming_coord_serve_is_none_not_a_coin_flip() -> None:
    """Two live daemons is a fault in its own right. A caller that has to
    order a roll around "the" daemon must refuse, not pick one."""
    assert rv.daemon_host_from_health({
        "a": _health(_spawns("coord-serve", RELEASED)),
        "b": _health(_spawns("coord-serve", RELEASED)),
    }) is None


def test_gather_labels_the_daemon_lane_with_the_real_machine_name(monkeypatch) -> None:
    """A lane labelled "daemon" cannot be matched to a host by anything
    downstream — which is how propagation ended up guessing at config order."""
    class _M:
        def __init__(self, name): self.name = name; self.host = name

    class _Status:
        is_online = True
        health = _health(_agent_venv(RELEASED), _spawns("coord-serve", RELEASED))

    class _Cfg:
        machines = [_M("dellserver")]

    _health_map, _unreachable, _facts, name = rv.gather(
        _Cfg(),
        check_machine=lambda machine, **kw: _Status(),
        board_payload=lambda: {},
    )
    assert name == "dellserver"


# ──────────────────────────────────────────────────────────────────────────
# #2121: a process whose code changed underneath it is not "behind"
# ──────────────────────────────────────────────────────────────────────────


def _mixed_health(*results: dict, running: str, installed: str) -> dict:
    """An agent `/health` body reporting two different self-versions.

    `version` is the module this process loaded at import time;
    `installed_version` is a fresh `importlib.metadata` read of the
    site-packages that same process resolves through (coord/agent_app.py).
    """
    body = _health(*results)
    body["version"] = running
    body["installed_version"] = installed
    return body


def test_mixed_version_process_reads_differently_from_merely_behind() -> None:
    """#2121 acceptance 4. On 2026-08-11 `coord release verify` graded
    dellserver's rewritten-underneath agent as a routine `CRIT ... on
    0.5.36, expected 0.5.37` — the identical line a host that simply hasn't
    been rolled yet gets. A process running code that no longer exists on
    disk is a different and worse condition and must not be spelled the
    same way."""
    behind = rv.verify(
        machine_health={"precision": _health(_agent_venv(STALE))},
        expected=RELEASED,
    )
    mixed = rv.verify(
        machine_health={
            "dellserver": _mixed_health(
                _agent_venv(RELEASED), running=STALE, installed=RELEASED,
            )
        },
        expected=RELEASED,
    )

    behind_text = rv.render(behind)
    mixed_text = rv.render(mixed)

    # The lagging host says nothing about mixed versions...
    assert "MIXED-VERSION PROCESS" not in behind_text
    assert "REPLACED UNDERNEATH IT" not in behind_text
    # ...and the rewritten one is unmistakable, naming both versions.
    assert "MIXED-VERSION PROCESS" in mixed_text
    assert f"running v{STALE}" in mixed_text
    assert f"install that is now v{RELEASED}" in mixed_text
    assert "REPLACED UNDERNEATH IT" in mixed_text
    assert mixed.severity == "crit"


def test_mixed_version_finding_survives_with_no_expected_version() -> None:
    """The 2026-08-11 run had no `--expected` to grade against — the finding
    must not depend on one, since the skew is inside a single host."""
    report = rv.verify(
        machine_health={
            "dellserver": _mixed_health(
                _agent_venv(RELEASED), running=STALE, installed=RELEASED,
            )
        },
    )
    mixed = [f for f in report.findings if "MIXED-VERSION PROCESS" in f.summary]
    assert len(mixed) == 1
    assert mixed[0].severity == "crit"
    assert mixed[0].host == "dellserver"


def test_agreeing_self_versions_produce_no_mixed_version_finding() -> None:
    """A correct blue/green swap leaves a live process pinned to the slot it
    started from, so BOTH reads stay on the old version until it restarts —
    that must not be reported as a rewritten install."""
    report = rv.verify(
        machine_health={
            "dellserver": _mixed_health(
                _agent_venv(RELEASED), running=STALE, installed=STALE,
            )
        },
    )
    assert not [f for f in report.findings if "MIXED-VERSION" in f.summary]


def test_an_agent_that_reports_only_one_self_version_is_not_a_finding() -> None:
    """Older agents publish `version` but not `installed_version`; absence is
    no data, never a mixed-version claim."""
    report = rv.verify(machine_health={"old": _health(_agent_venv(RELEASED))})
    assert not [f for f in report.findings if "MIXED-VERSION" in f.summary]
