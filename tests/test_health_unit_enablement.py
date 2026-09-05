"""Unit tests for the `unit_enablement` machine-scope health check (#2098).

`unit_drift` (tests: `tests/test_health_unit_drift.py`) already covers
"does the installed unit's content match the release". This module covers
the orthogonal question that actually cost a day: an installed unit whose
content is byte-perfect can still be `disabled`, and that state produces
no evidence until something needed it — a disabled timer and a deferring
timer both look like silence. Per #2096, the failing verdict is exercised
directly here, not just described.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import HealthConfig
from coord.deploy_manifest import ROLE_DAEMON, ROLE_WORKER, RoleDeclaration, units_for_role
from coord.health.checks import unit_enablement as ue
from coord.health.models import Checkout, HealthContext, Severity

NOW = 1_800_000_000.0


def make_ctx(tmp_path: Path, **kwargs) -> HealthContext:
    thresholds = kwargs.pop("thresholds", None) or HealthConfig()
    home = kwargs.pop("home", tmp_path)
    return HealthContext(
        thresholds=thresholds,
        home=home,
        coord_dir=kwargs.pop("coord_dir", home / ".coord"),
        now=kwargs.pop("now", NOW),
        checkouts=kwargs.pop("checkouts", ()),
        config=kwargs.pop("config", None),
        allow_network=kwargs.pop("allow_network", True),
    )


class _FakeProc:
    def __init__(self, stdout: str = "", stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(states: dict[str, str]):
    """A `subprocess.run`-shaped fake: `systemctl --user is-enabled <unit>`
    returns `states[unit]` on stdout, mirroring what real systemctl does —
    the state on stdout regardless of exit code."""

    def run(cmd, **kwargs):
        unit = cmd[-1]
        return _FakeProc(stdout=states.get(unit, "not-found") + "\n")

    return run


def _install(tmp_path: Path, *names: str) -> Path:
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (installed_dir / name).write_text("[Unit]\n")
    return installed_dir


# ── _is_enabled ──────────────────────────────────────────────────────────


def test_is_enabled_reads_stdout_regardless_of_returncode() -> None:
    state, error = ue._is_enabled(
        "coord-release-propagate.timer",
        runner=_fake_runner({"coord-release-propagate.timer": "disabled"}),
    )
    assert state == "disabled"
    assert error is None


def test_is_enabled_reports_missing_systemctl() -> None:
    def run(cmd, **kwargs):
        raise FileNotFoundError("systemctl")

    state, error = ue._is_enabled("coord-agent.service", runner=run)
    assert state is None
    assert "no systemd" in error


# ── probe_unit_enablement ────────────────────────────────────────────────


def test_no_manifest_unit_installed_is_ok(tmp_path) -> None:
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert len(results) == 1
    assert results[0].severity is Severity.OK
    assert "no manifest-listed unit installed" in results[0].headroom


def test_uninstalled_manifest_unit_is_silently_skipped(tmp_path, monkeypatch) -> None:
    """A worker box that never installed the daemon-only lanes is not a
    fault — same "don't guess topology" boundary as `unit_drift`."""
    _install(tmp_path, "coord-agent.service")
    monkeypatch.setattr(
        ue, "_is_enabled", lambda name, **kw: ("enabled", None)
    )
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert [r.subject for r in results] == ["coord-agent.service"]


def test_installed_and_enabled_unit_is_ok(tmp_path, monkeypatch) -> None:
    _install(tmp_path, "coord-release-propagate.timer")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("enabled", None))
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert len(results) == 1
    r = results[0]
    assert r.subject == "coord-release-propagate.timer"
    assert r.severity is Severity.OK
    assert r.headroom == "enabled"


def test_installed_but_disabled_unit_fails(tmp_path, monkeypatch) -> None:
    """The exact #2098 incident, reproduced: a unit that is `cp`'d onto a
    host and byte-identical to the release, but never `enable --now`'d.
    This is the failing verdict #2096 requires be exercised, not just
    described — assert it actually fires."""
    _install(tmp_path, "coord-release-propagate.timer")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("disabled", None))
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert len(results) == 1
    r = results[0]
    assert r.subject == "coord-release-propagate.timer"
    assert r.severity is Severity.WARN
    assert "disabled" in r.headroom
    assert "enable --now coord-release-propagate.timer" in r.detail


def test_installed_but_masked_unit_fails(tmp_path, monkeypatch) -> None:
    _install(tmp_path, "coord-db-backup.timer")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("masked", None))
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert results[0].severity is Severity.WARN


def test_is_enabled_error_is_unknown_not_ok_or_warn(tmp_path, monkeypatch) -> None:
    _install(tmp_path, "coord-serve.service")
    monkeypatch.setattr(
        ue, "_is_enabled", lambda name, **kw: (None, "systemctl not found (no systemd on this host)")
    )
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert len(results) == 1
    assert results[0].severity is Severity.UNKNOWN
    assert results[0].error


def test_multiple_installed_units_report_independently(tmp_path, monkeypatch) -> None:
    _install(tmp_path, "coord-serve.service", "coord-notify.timer", "coord-db-backup.timer")
    states = {
        "coord-serve.service": "enabled",
        "coord-notify.timer": "disabled",
        "coord-db-backup.timer": "enabled",
    }
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: (states[name], None))
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    by_subject = {r.subject: r for r in results}
    assert by_subject["coord-serve.service"].severity is Severity.OK
    assert by_subject["coord-notify.timer"].severity is Severity.WARN
    assert by_subject["coord-db-backup.timer"].severity is Severity.OK


def test_resolve_systemd_user_dir_honors_configured_path(tmp_path, monkeypatch) -> None:
    configured = tmp_path / "custom" / "systemd"
    configured.mkdir(parents=True)
    (configured / "coord-serve.service").write_text("[Unit]\n")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("enabled", None))
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(systemd_user_dir=str(configured)))
    results = ue.probe_unit_enablement(ctx)
    assert [r.subject for r in results] == ["coord-serve.service"]


# ── declared role (#3128) ────────────────────────────────────────────────
#
# `resolve_role` (`coord/deploy_manifest.py`) is the single source of truth
# for "what role does this host play" — these tests monkeypatch
# `ue.resolve_role` directly rather than writing real `~/.coord/role`
# files/env vars, both to stay deterministic and (see
# `test_check_does_not_reread_role_declaration_itself` below) to prove the
# check consumes the resolver's answer instead of re-parsing the source.


def _declare(role: str, *, source: str, valid: bool = True, raw: str | None = None) -> RoleDeclaration:
    return RoleDeclaration(role=role, raw=raw, source=source, valid=valid)


def test_undeclared_role_adds_no_new_warnings(tmp_path, monkeypatch) -> None:
    """#3128 acceptance 1: a host that never declared a role must behave
    byte-identically to pre-#3128 — no missing-unit WARN even though every
    daemon-only manifest unit (coord-backup.timer, coord-dr-verify.timer,
    ...) is absent, because absence with no declared role is still just
    "not this host's topology" (#1831), unchanged by this issue."""
    _install(tmp_path, "coord-agent.service")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("enabled", None))
    monkeypatch.setattr(
        ue, "resolve_role", lambda coord_dir: _declare(ROLE_WORKER, source="default")
    )
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert [r.subject for r in results] == ["coord-agent.service"]
    assert all(r.severity is Severity.OK for r in results)


def test_declared_daemon_role_missing_backup_timer_warns(tmp_path, monkeypatch) -> None:
    """#3128 acceptance 2: a host that HAS declared `daemon` and is missing
    `coord-backup.timer` gets a WARN naming it — the exact fault #3118/#3119
    shipped invisibly."""
    _install(tmp_path, "coord-agent.service")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("enabled", None))
    monkeypatch.setattr(
        ue, "resolve_role", lambda coord_dir: _declare(ROLE_DAEMON, source="file", raw="daemon")
    )
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    by_subject = {r.subject: r for r in results}
    # `subject` carries the unit name (the row label is "unit enablement
    # <subject>") — the headroom text itself explains *why*, referencing the
    # declared role rather than repeating the name subject already carries.
    assert by_subject["coord-backup.timer"].severity is Severity.WARN
    assert "'daemon'" in by_subject["coord-backup.timer"].headroom
    assert by_subject["coord-dr-verify.timer"].severity is Severity.WARN


def test_missing_unit_warn_fix_line_installs_and_enables_from_packaged_copy(
    tmp_path, monkeypatch
) -> None:
    """#3128 acceptance 3: the fix line is a real, runnable command sourced
    from the packaged reference — `unit_drift`'s own convention — not a bare
    pointer at documentation."""
    _install(tmp_path, "coord-agent.service")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("enabled", None))
    monkeypatch.setattr(
        ue, "resolve_role", lambda coord_dir: _declare(ROLE_DAEMON, source="file", raw="daemon")
    )
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    detail = {r.subject: r.detail for r in results}["coord-backup.timer"]
    assert "cp " in detail
    assert detail.count("coord-backup.timer") >= 2  # source name and install target
    assert "systemctl --user daemon-reload" in detail
    assert "systemctl --user enable --now coord-backup.timer" in detail


def test_missing_templated_unit_remedy_actually_enables_it(tmp_path, monkeypatch) -> None:
    """Review finding (fix iteration 1): `coord-agent.service` is the one
    manifest unit that is a systemd *template* (#1928's `<MACHINE_NAME>`/
    `<PORT>`), so a required-but-missing `coord-agent.service` takes the
    templated branch of `_missing_unit_remedy`. The previous shape of that
    branch reused `unit_drift._templated_remedy` verbatim (which ends in
    `systemctl --user restart {service}` — correct for ITS caller, where the
    unit is already enabled and only its content drifted) and appended
    `enable --now` only after a trailing `#`. Everything after `#` is a
    shell comment: copy-pasting the whole detail string (the entire point of
    a fix line) would render+reload+restart but never actually enable the
    unit — reproducing the exact "disabled unit produces zero evidence"
    failure shape (#2098) this check exists to catch. The fix line must
    instead end in a real, chained `enable --now coord-agent.service`."""
    # coord-agent.service is never installed on this host at all.
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("enabled", None))
    monkeypatch.setattr(
        ue, "resolve_role", lambda coord_dir: _declare(ROLE_WORKER, source="file", raw="worker")
    )
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    detail = {r.subject: r.detail for r in results}["coord-agent.service"]
    enable_cmd = "systemctl --user enable --now coord-agent.service"
    assert "sed " in detail  # #1928: rendered from the template, never a bare cp
    assert "systemctl --user daemon-reload" in detail
    assert enable_cmd in detail
    # The enable command must be part of the executable `&&` chain right
    # after the daemon-reload step — not sitting after a `#`, which would
    # make it an inert comment instead of something that actually runs.
    reload_idx = detail.index("systemctl --user daemon-reload")
    enable_idx = detail.index(enable_cmd)
    assert reload_idx < enable_idx
    assert "#" not in detail[reload_idx:enable_idx]


def test_masked_required_unit_is_not_reported_as_never_installed(tmp_path, monkeypatch) -> None:
    """Non-blocking review concern: a role-required unit the fleet
    deliberately masks (`coord-release-propagate.timer` on dellserver, per
    the manual-release-rolls policy) must not be reported as "never
    installed" by the #3128 branch. `systemctl --user mask` leaves a symlink
    to `/dev/null` at the installed path, and `/dev/null` exists — so
    `installed_path.exists()` is True and this unit never enters the
    "required but missing" branch at all; it falls through to the
    pre-existing "installed but not enabled" WARN path, unchanged by #3128.
    That falls out of the code by construction, but was unasserted."""
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    masked = installed_dir / "coord-release-propagate.timer"
    masked.symlink_to("/dev/null")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("masked", None))
    monkeypatch.setattr(
        ue, "resolve_role", lambda coord_dir: _declare(ROLE_DAEMON, source="file", raw="daemon")
    )
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    by_subject = {r.subject: r for r in results}
    r = by_subject["coord-release-propagate.timer"]
    assert r.severity is Severity.WARN
    assert r.values.get("required") is not True
    assert "never installed" not in r.headroom
    assert r.values["state"] == "masked"


def test_declared_daemon_role_fully_installed_and_enabled_is_ok(tmp_path, monkeypatch) -> None:
    """#3128 acceptance 4: a daemon host with every manifest unit installed
    AND enabled reports OK across the board — the new branch never fires a
    false positive against a correctly-provisioned host."""
    daemon_units = units_for_role(ROLE_DAEMON)
    _install(tmp_path, *daemon_units)
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("enabled", None))
    monkeypatch.setattr(
        ue, "resolve_role", lambda coord_dir: _declare(ROLE_DAEMON, source="file", raw="daemon")
    )
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert results
    assert {r.subject for r in results} == set(daemon_units)
    assert all(r.severity is Severity.OK for r in results)


def test_unrecognized_declared_role_warns_and_falls_back_to_worker(tmp_path, monkeypatch) -> None:
    """#3128 acceptance 5: an unparseable/unknown role value never raises
    and never silently reads as `daemon` — it WARNs about the bad value and
    falls back to `worker`'s (already-satisfied) requirement set."""
    _install(tmp_path, "coord-agent.service")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("enabled", None))
    monkeypatch.setattr(
        ue,
        "resolve_role",
        lambda coord_dir: _declare(ROLE_WORKER, source="file", raw="production", valid=False),
    )
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    by_subject = {r.subject: r for r in results}
    role_warnings = [r for r in results if r.subject == "role"]
    assert len(role_warnings) == 1
    assert role_warnings[0].severity is Severity.WARN
    assert "production" in role_warnings[0].headroom
    assert "worker" in role_warnings[0].headroom
    # Falls back to the worker requirement set (coord-agent.service, already
    # installed) — no missing-unit WARN piles on top of the bad-value WARN.
    assert set(by_subject) == {"role", "coord-agent.service"}
    assert by_subject["coord-agent.service"].severity is Severity.OK


def test_check_never_executes_mutating_commands(tmp_path, monkeypatch) -> None:
    """#3128 acceptance 6: this check only ever *describes* a remedy — it
    must never itself run `systemctl enable`, `systemctl start`, `cp`, or
    `apt`. Patches `subprocess.run` (not `_is_enabled`) so the real
    `is-enabled` probing still happens and is asserted to be the ONLY
    subprocess this check invokes."""
    _install(tmp_path, "coord-agent.service", "coord-serve.service")
    monkeypatch.setattr(
        ue, "resolve_role", lambda coord_dir: _declare(ROLE_DAEMON, source="file", raw="daemon")
    )
    calls: list[list[str]] = []

    def spy_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeProc(stdout="enabled\n")

    monkeypatch.setattr(ue.subprocess, "run", spy_run)
    results = ue.probe_unit_enablement(make_ctx(tmp_path))

    # The missing-unit branch fired (proving this exercised the code path
    # this criterion is guarding, not a no-op).
    assert any(
        r.subject == "coord-backup.timer" and r.severity is Severity.WARN for r in results
    )
    forbidden = {"enable", "start", "cp", "apt"}
    assert calls, "expected at least one systemctl is-enabled probe"
    for call in calls:
        assert call[:3] == ["systemctl", "--user", "is-enabled"], call
        assert not (set(call) & forbidden), f"check executed a mutating command: {call}"


def test_check_does_not_reread_role_declaration_itself(tmp_path, monkeypatch) -> None:
    """#3128 acceptance 7: `resolve_role` is the only function that reads
    `~/.coord/role`/`COORD_ROLE` — proven by writing a REAL role file the
    check would see if it parsed the source itself, while forcing the
    monkeypatched `resolve_role` to answer "nothing declared". The check
    must follow the resolver's answer, not the filesystem underneath it."""
    coord_dir = tmp_path / ".coord"
    coord_dir.mkdir(parents=True)
    (coord_dir / "role").write_text("daemon\n")  # a real file saying "daemon"
    _install(tmp_path, "coord-agent.service")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("enabled", None))
    monkeypatch.setattr(
        ue, "resolve_role", lambda coord_dir: _declare(ROLE_WORKER, source="default")
    )
    results = ue.probe_unit_enablement(make_ctx(tmp_path, coord_dir=coord_dir))
    # If the check re-read the file itself it would see "daemon" and warn
    # about every missing daemon-only unit. It must not: it only ever sees
    # what resolve_role handed back.
    assert [r.subject for r in results] == ["coord-agent.service"]
    assert all(r.severity is Severity.OK for r in results)
