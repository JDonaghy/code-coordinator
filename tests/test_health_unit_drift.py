"""Unit tests for the `unit_drift` machine-scope health check (#1831).

Mirrors `tests/test_health_deploy_lane_facts.py`'s structure — see that
module's docstring for the "measure locally, judge centrally" pattern this
check follows. Covers both halves of #1831's acceptance criteria: content
drift (installed != deploy/) and PATH shadow risk (an editable checkout
ahead of the release entry point).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coord.config import HealthConfig
from coord.health.checks import unit_drift as ud
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


UNIT_TEXT = (
    "[Service]\n"
    "Type=simple\n"
    "Environment=PATH=%h/.cargo/bin:%h/.local/bin:/usr/bin:/bin\n"
    "ExecStart=%h/.coord-venv/bin/coord serve\n"
)


def _make_deploy_dir(tmp_path: Path, units: dict[str, str]) -> Path:
    deploy_dir = tmp_path / "checkout" / "deploy"
    deploy_dir.mkdir(parents=True)
    for name, text in units.items():
        (deploy_dir / name).write_text(text)
    return deploy_dir


@pytest.fixture(autouse=True)
def no_packaged_units(monkeypatch):
    """Default every test to "this install ships no packaged units".

    Without this the probe would find THIS repo's real `coord/deploy/`
    (#1927) instead of whatever the test built under `tmp_path`. Tests that
    care about the packaged reference opt in with :func:`use_packaged`.
    """
    monkeypatch.setattr(ud, "packaged_unit_dir", lambda: None)


def use_packaged(monkeypatch, path: Path, *, verified: bool = True, version="0.4.110"):
    """Point the probe at `path` as the packaged (released) reference."""
    monkeypatch.setattr(ud, "packaged_unit_dir", lambda: path)
    monkeypatch.setattr(ud, "in_git_worktree", lambda _p: not verified)
    monkeypatch.setattr(ud, "installed_version", lambda: version)


# ── resolve_deploy_dir / resolve_systemd_user_dir ──────────────────────────


def test_resolve_deploy_dir_is_none_with_no_checkout(tmp_path) -> None:
    assert ud.resolve_deploy_dir(make_ctx(tmp_path)) is None


def test_resolve_deploy_dir_finds_first_checkout_with_one(tmp_path) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    deploy = checkout / "deploy"
    deploy.mkdir(parents=True)
    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    assert ud.resolve_deploy_dir(ctx) == deploy


def test_resolve_deploy_dir_prefers_configured_path(tmp_path) -> None:
    configured = tmp_path / "elsewhere" / "deploy"
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(deploy_dir=str(configured)))
    assert ud.resolve_deploy_dir(ctx) == configured


def test_resolve_systemd_user_dir_default(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    assert ud.resolve_systemd_user_dir(ctx) == tmp_path / ".config" / "systemd" / "user"


def test_resolve_systemd_user_dir_prefers_configured_path(tmp_path) -> None:
    configured = tmp_path / "custom" / "systemd"
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(systemd_user_dir=str(configured)))
    assert ud.resolve_systemd_user_dir(ctx) == configured


# ── probe_unit_drift ─────────────────────────────────────────────────────


def test_no_deploy_dir_is_ok_not_unknown(tmp_path) -> None:
    results = ud.probe_unit_drift(make_ctx(tmp_path))
    assert len(results) == 1
    assert results[0].severity is Severity.OK
    assert "no deploy/ checkout" in results[0].headroom


def test_unit_not_installed_is_ok(tmp_path) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    _make_deploy_dir(tmp_path, {"coord-serve.service": UNIT_TEXT})
    (checkout / "deploy").mkdir(parents=True, exist_ok=True)
    (checkout / "deploy" / "coord-serve.service").write_text(UNIT_TEXT)
    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    results = ud.probe_unit_drift(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.subject == "coord-serve.service"
    assert r.severity is Severity.OK
    assert r.headroom == "not installed on this machine"
    assert r.values["installed"] is False


def test_matching_unit_is_ok_and_silent(tmp_path, monkeypatch) -> None:
    """The acceptance-criteria "matching unit -> silent" half.

    Only a *verified* reference — the units packaged with the installed
    release — can produce this green (#1927).
    """
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-serve.service").write_text(UNIT_TEXT)
    use_packaged(monkeypatch, packaged)
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    (installed_dir / "coord-serve.service").write_text(UNIT_TEXT)

    results = ud.probe_unit_drift(make_ctx(tmp_path))
    assert len(results) == 1
    r = results[0]
    assert r.severity is Severity.OK
    assert r.headroom == "matches packaged coord 0.4.110"
    assert r.values["matches"] is True
    assert r.values["reference_verified"] is True
    assert r.values["reference_source"] == "package"
    assert r.values["reference_version"] == "0.4.110"


def test_stale_unit_is_warn_and_reports_mtime_and_diff(tmp_path) -> None:
    """The acceptance-criteria "stale unit -> reported" half."""
    checkout = tmp_path / "src" / "claude-coordinator"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "deploy" / "coord-serve.service").write_text(
        UNIT_TEXT + "ExtraLineInDeploy=1\n"
    )
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    installed = installed_dir / "coord-serve.service"
    installed.write_text(UNIT_TEXT)
    stale_mtime = NOW - (21 * 24 * 3600)  # three weeks stale, matches #1831
    os.utime(installed, (stale_mtime, stale_mtime))

    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    results = ud.probe_unit_drift(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.severity is Severity.WARN
    assert "stale" in r.headroom
    assert "line" in r.headroom
    assert r.values["installed_mtime"] == pytest.approx(stale_mtime)
    assert r.values["diff_lines"] >= 1
    assert "cp" in r.detail and "restart" in r.detail


def test_stale_unit_with_no_sentinel_entry_is_not_suppressed(tmp_path) -> None:
    """#3049: absence of a sentinel entry must not read as "suppressed" —
    the sentinel is the signal, not the masking."""
    checkout = tmp_path / "src" / "claude-coordinator"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "deploy" / "coord-release-propagate.service").write_text(
        UNIT_TEXT + "ExtraLineInDeploy=1\n"
    )
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    installed = installed_dir / "coord-release-propagate.service"
    installed.write_text(UNIT_TEXT)

    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    results = ud.probe_unit_drift(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.severity is Severity.WARN
    assert r.values["suppressed"] is False
    assert r.values["suppress_reason"] is None
    assert r.values["suppress_set"] is None


def test_stale_unit_covered_by_the_watchdog_suppress_sentinel_is_flagged(tmp_path) -> None:
    """#3049: a unit masked on purpose — covered by the same
    ``watchdog-suppress.json`` sentinel the fleet watchdog already honours —
    still reports the honest WARN (severity is this probe's call, not a
    policy layer's), but surfaces the sentinel's reason/set date in
    ``values`` so a policy-aware consumer (`coord release verify`, #3049)
    can render it as masked-by-policy instead of paging on it.
    """
    checkout = tmp_path / "src" / "claude-coordinator"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "deploy" / "coord-release-propagate.service").write_text(
        UNIT_TEXT + "ExtraLineInDeploy=1\n"
    )
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    installed = installed_dir / "coord-release-propagate.service"
    installed.write_text(UNIT_TEXT)

    coord_dir = tmp_path / ".coord"
    coord_dir.mkdir()
    (coord_dir / "watchdog-suppress.json").write_text(
        json.dumps(
            {
                "coord-release-propagate.service": {
                    "reason": "manual release rolls by choice -- masked, not broken",
                    "set": "2026-08-26",
                    "expires": None,
                }
            }
        )
    )

    ctx = make_ctx(
        tmp_path,
        coord_dir=coord_dir,
        checkouts=(Checkout(name="coordinator", path=checkout),),
    )
    results = ud.probe_unit_drift(ctx)
    assert len(results) == 1
    r = results[0]
    # Still the honest WARN — this probe has no policy context of its own.
    assert r.severity is Severity.WARN
    assert r.values["suppressed"] is True
    assert r.values["suppress_reason"] == "manual release rolls by choice -- masked, not broken"
    assert r.values["suppress_set"] == "2026-08-26"


def test_unreadable_installed_unit_is_unknown(tmp_path) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "deploy" / "coord-serve.service").write_text(UNIT_TEXT)
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    installed = installed_dir / "coord-serve.service"
    installed.mkdir()  # a directory, not a file -> read_text() raises

    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    results = ud.probe_unit_drift(ctx)
    assert len(results) == 1
    assert results[0].severity is Severity.UNKNOWN
    assert results[0].error


def test_multiple_units_each_get_their_own_result(tmp_path, monkeypatch) -> None:
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-serve.service").write_text(UNIT_TEXT)
    (packaged / "coord-agent.service").write_text(UNIT_TEXT)
    (packaged / "coord-notify.timer").write_text("[Timer]\n")
    use_packaged(monkeypatch, packaged)

    results = ud.probe_unit_drift(make_ctx(tmp_path))
    subjects = sorted(r.subject for r in results)
    assert subjects == [
        "coord-agent.service",
        "coord-notify.timer",
        "coord-serve.service",
    ]
    assert all(r.severity is Severity.OK for r in results)


# ── find_path_shadow ─────────────────────────────────────────────────────


def test_no_path_line_has_no_shadow() -> None:
    assert ud.find_path_shadow("[Service]\nExecStart=/bin/true\n") is None


def test_release_first_has_no_shadow() -> None:
    text = (
        "Environment=PATH=%h/.cargo/bin:%h/.local/bin:"
        "%h/src/claude-coordinator/.venv/bin:/usr/bin\n"
    )
    assert ud.find_path_shadow(text) is None


def test_editable_venv_before_local_bin_is_a_shadow() -> None:
    """The exact #1831 dellserver shape: the repo venv ahead of ~/.local/bin."""
    text = "Environment=PATH=%h/src/claude-coordinator/.venv/bin:%h/.local/bin:/usr/bin\n"
    assert ud.find_path_shadow(text) == "%h/src/claude-coordinator/.venv/bin"


def test_editable_venv_before_coord_venv_bin_is_a_shadow() -> None:
    text = "Environment=PATH=%h/src/claude-coordinator/.venv/bin:%h/.coord-venv/bin:/usr/bin\n"
    assert ud.find_path_shadow(text) == "%h/src/claude-coordinator/.venv/bin"


def test_coord_venv_and_coord_cli_venv_are_not_mistaken_for_a_dev_venv() -> None:
    """`.coord-venv`/`.coord-cli-venv` are the SANCTIONED venvs — a probe
    that flagged them as `.venv/bin` would make every stock install CRIT."""
    text = "Environment=PATH=%h/.coord-cli-venv/bin:%h/.coord-venv/bin:%h/.local/bin\n"
    assert ud.find_path_shadow(text) is None


def test_only_the_last_environment_path_line_is_read() -> None:
    text = (
        "Environment=PATH=%h/.local/bin:/usr/bin\n"
        "Environment=PATH=%h/src/claude-coordinator/.venv/bin:%h/.local/bin\n"
    )
    assert ud.find_path_shadow(text) == "%h/src/claude-coordinator/.venv/bin"


def test_shadow_risk_wins_over_content_match(tmp_path) -> None:
    """Even a unit whose content is byte-identical to deploy/ must CRIT if
    deploy/ itself regresses the PATH ordering — #1831's exact shape: the
    v0.4.105 cut of coord-serve.service was internally consistent and still
    wrong."""
    text = "Environment=PATH=%h/src/claude-coordinator/.venv/bin:%h/.local/bin:/usr/bin\n"
    checkout = tmp_path / "src" / "claude-coordinator"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "deploy" / "coord-serve.service").write_text(text)
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    (installed_dir / "coord-serve.service").write_text(text)

    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    results = ud.probe_unit_drift(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.severity is Severity.CRIT
    assert r.values["matches"] is True
    assert r.values["shadow_entry"] == "%h/src/claude-coordinator/.venv/bin"


# ── #1927: the reference must be the released artifact ────────────────────
#
# The check diffed installed units against `<checkout>/deploy/<name>` — a
# file in the host's own working copy that nothing verifies is current.
# Installed units and checkouts go stale for the same reason (nobody
# pulled), so they go stale together and the diff reports clean: the check
# was least reliable in exactly the #1831 case it exists to catch.


RELEASED_UNIT = UNIT_TEXT + "Environment=COORD_RELEASED=1\n"


def _install(tmp_path: Path, name: str, text: str) -> Path:
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True, exist_ok=True)
    path = installed_dir / name
    path.write_text(text)
    return path


def _stale_checkout(tmp_path: Path, name: str, text: str) -> Path:
    """A git checkout parked days behind the release — the false-green half."""
    checkout = tmp_path / "src" / "claude-coordinator"
    (checkout / "deploy").mkdir(parents=True, exist_ok=True)
    (checkout / "deploy" / name).write_text(text)
    return checkout


def test_stale_checkout_and_stale_unit_that_agree_still_report_drift(
    tmp_path, monkeypatch
) -> None:
    """The direct #1831 regression: both sides stale, so they MATCH.

    dellserver ran a `coord-serve.service` whose PATH had been wrong for
    591.8h while its checkout sat days behind. Diffed against that checkout
    the installed unit was byte-identical and the tool reported clean.
    Against the packaged release it is drift, which is the point.
    """
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-serve.service").write_text(RELEASED_UNIT)
    use_packaged(monkeypatch, packaged)

    checkout = _stale_checkout(tmp_path, "coord-serve.service", UNIT_TEXT)
    installed = _install(tmp_path, "coord-serve.service", UNIT_TEXT)
    # The old reference and the installed unit agree exactly.
    assert (checkout / "deploy" / "coord-serve.service").read_text() == (
        installed.read_text()
    )

    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    results = ud.probe_unit_drift(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.severity is Severity.WARN
    assert r.values["reference_source"] == "package"
    assert r.values["reference_verified"] is True
    assert r.values["diff_lines"] >= 1


def test_stale_checkout_with_a_correct_unit_does_not_report_drift(
    tmp_path, monkeypatch
) -> None:
    """The observed 2026-08-07 false red: the *reference* was the stale side.

    A unit installed 0.0h ago from the released tag reported "stale — 42
    line(s) differ" because dellserver's checkout was days behind.
    """
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-serve.service").write_text(RELEASED_UNIT)
    use_packaged(monkeypatch, packaged)

    checkout = _stale_checkout(tmp_path, "coord-serve.service", UNIT_TEXT)
    _install(tmp_path, "coord-serve.service", RELEASED_UNIT)

    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    results = ud.probe_unit_drift(ctx)
    assert len(results) == 1
    assert results[0].severity is Severity.OK
    assert results[0].values["matches"] is True


def test_remedy_sources_from_the_verified_reference_not_the_checkout(
    tmp_path, monkeypatch
) -> None:
    """`cp` out of an unverified checkout is how a stale unit got cemented."""
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-serve.service").write_text(RELEASED_UNIT)
    use_packaged(monkeypatch, packaged)

    checkout = _stale_checkout(tmp_path, "coord-serve.service", "[Service]\nstale=1\n")
    _install(tmp_path, "coord-serve.service", UNIT_TEXT)

    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    (r,) = ud.probe_unit_drift(ctx)
    assert r.severity is Severity.WARN
    assert str(packaged / "coord-serve.service") in r.detail
    assert str(checkout) not in r.detail
    assert r.values["deploy_path"] == str(packaged / "coord-serve.service")


def test_match_against_an_unverified_working_copy_is_unknown_not_ok(
    tmp_path, monkeypatch
) -> None:
    """An un-annotated green from an unverified reference is worse than no
    check, so a match the tool cannot vouch for grades UNKNOWN."""
    checkout = _stale_checkout(tmp_path, "coord-serve.service", UNIT_TEXT)
    _install(tmp_path, "coord-serve.service", UNIT_TEXT)

    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    (r,) = ud.probe_unit_drift(ctx)
    assert r.severity is Severity.UNKNOWN
    assert r.values["matches"] is True
    assert r.values["reference_source"] == "checkout"
    assert r.values["reference_verified"] is False
    assert "unverified working copy" in r.headroom
    assert str(checkout) in r.detail


def test_source_checkout_install_is_an_unverified_reference(
    tmp_path, monkeypatch
) -> None:
    """`coord/deploy/` inside an EDITABLE install is still a working copy —
    the package dir sits under a `.git`, so it gets no green either."""
    packaged = tmp_path / "src" / "claude-coordinator" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-serve.service").write_text(UNIT_TEXT)
    use_packaged(monkeypatch, packaged, verified=False)
    _install(tmp_path, "coord-serve.service", UNIT_TEXT)

    (r,) = ud.probe_unit_drift(make_ctx(tmp_path))
    assert r.severity is Severity.UNKNOWN
    assert r.values["reference_source"] == "package"
    assert r.values["reference_verified"] is False


def test_configured_deploy_dir_is_only_a_fallback_and_is_unverified(
    tmp_path, monkeypatch
) -> None:
    """`health.deploy_dir` still points the check somewhere when the wheel
    ships no units, but it names a host path nothing verifies."""
    configured = tmp_path / "elsewhere" / "deploy"
    configured.mkdir(parents=True)
    (configured / "coord-serve.service").write_text(UNIT_TEXT)
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(deploy_dir=str(configured)))
    ref = ud.resolve_reference(ctx)
    assert ref is not None
    assert ref.source == "configured"
    assert ref.verified is False

    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-serve.service").write_text(RELEASED_UNIT)
    use_packaged(monkeypatch, packaged)
    ref = ud.resolve_reference(ctx)
    assert ref is not None
    assert ref.source == "package"
    assert ref.path == packaged


# ── the real packaged reference ───────────────────────────────────────────


def test_this_distribution_ships_its_reference_units() -> None:
    """Not a mock: the distribution must actually carry
    `coord/deploy/*.service`, or every host silently falls back to its own
    working copy. Resolved without `packaged_unit_dir` (which the autouse
    fixture stubs out) so it asserts the real layout."""
    import coord

    packaged = Path(coord.__file__).resolve().parent / "deploy"
    assert packaged.is_dir()
    names = {p.name for p in ud._unit_files(packaged)}
    assert "coord-serve.service" in names
    assert "coord-agent.service" in names


def test_in_git_worktree_distinguishes_a_checkout_from_site_packages(
    tmp_path,
) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "coord" / "deploy").mkdir(parents=True)
    assert ud.in_git_worktree(checkout / "coord" / "deploy") is True

    site = tmp_path / "venv" / "lib" / "site-packages" / "coord" / "deploy"
    site.mkdir(parents=True)
    assert ud.in_git_worktree(site) is False


# ── #1928: coord-agent.service is a template, not a plain file ────────────
#
# `deploy/coord-agent.service` carries `<MACHINE_NAME>`/`<PORT>` placeholders
# that every real install fills in — a byte-diff against it warned on every
# host, permanently, and its remedy was a bare `cp` that (followed verbatim)
# installs the placeholders as literal text and takes the unit down. These
# mirror a shrunk version of the real template plus the two real install
# shapes described in the issue: a manual sed-install (keeps the ~76-line
# doc header and `%h`) and an install-agent.sh install (drops the header,
# expands `%h` to a literal $HOME, adds its own small PATH comment).

TEMPLATE_UNIT = (
    "# some doc comment\n"
    "# another doc comment\n"
    "\n"
    "[Unit]\n"
    "Description=Coordinator agent server (port <PORT>)\n"
    "After=network-online.target\n"
    "\n"
    "[Service]\n"
    "Type=simple\n"
    "ExecStart=%h/.coord-venv/bin/coord agent --machine <MACHINE_NAME> --port <PORT>\n"
    "Restart=on-failure\n"
    "RestartSec=5\n"
    "Environment=PATH=%h/.coord-venv/bin:%h/.cargo/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
    "\n"
    "[Install]\n"
    "WantedBy=default.target\n"
)


def _sed_installed(home: Path, machine: str = "dellserver", port: str = "7433") -> str:
    """The manual-install shape: `<MACHINE_NAME>`/`<PORT>` filled in, `%h`
    and the doc-comment header left exactly as the template has them."""
    return TEMPLATE_UNIT.replace("<MACHINE_NAME>", machine).replace("<PORT>", port)


def _agent_installer_installed(home: Path, machine: str = "dellserver", port: str = "7433") -> str:
    """The install-agent.sh shape: no doc header, `%h` expanded to a literal
    $HOME, its own small PATH comment — see `install-agent.sh`'s heredoc."""
    return (
        "[Unit]\n"
        f"Description=Coordinator agent server (port {port})\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={home}/.coord-venv/bin/coord agent --machine {machine} --port {port}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "# #1671: include ~/.cargo/bin so a rustup-installed toolchain resolves\n"
        f"Environment=PATH={home}/.coord-venv/bin:{home}/.cargo/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def test_sed_installed_template_reports_no_drift(tmp_path, monkeypatch) -> None:
    """The documented manual install (sed `<MACHINE_NAME>`/`<PORT>`, leave
    `%h` and the header alone) must grade OK — acceptance criterion 1."""
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-agent.service").write_text(TEMPLATE_UNIT)
    use_packaged(monkeypatch, packaged)
    _install(tmp_path, "coord-agent.service", _sed_installed(tmp_path))

    (r,) = ud.probe_unit_drift(make_ctx(tmp_path))
    assert r.severity is Severity.OK
    assert r.values["matches"] is True
    assert r.values["templated"] is True


def test_install_agent_sh_installed_template_reports_no_drift(tmp_path, monkeypatch) -> None:
    """The real-world install path (install-agent.sh's heredoc) must also
    grade OK — this is the actual shape running on the fleet in #1928."""
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-agent.service").write_text(TEMPLATE_UNIT)
    use_packaged(monkeypatch, packaged)
    _install(tmp_path, "coord-agent.service", _agent_installer_installed(tmp_path))

    (r,) = ud.probe_unit_drift(make_ctx(tmp_path))
    assert r.severity is Severity.OK
    assert r.values["matches"] is True


def test_different_machine_and_port_values_still_match_consistently(tmp_path, monkeypatch) -> None:
    """`<PORT>` appears twice (Description and ExecStart) — both occurrences
    must resolve to the SAME value, but any value is acceptable."""
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-agent.service").write_text(TEMPLATE_UNIT)
    use_packaged(monkeypatch, packaged)
    _install(
        tmp_path,
        "coord-agent.service",
        _agent_installer_installed(tmp_path, machine="precision", port="7500"),
    )

    (r,) = ud.probe_unit_drift(make_ctx(tmp_path))
    assert r.severity is Severity.OK


def test_inconsistent_placeholder_values_still_report_drift(tmp_path, monkeypatch) -> None:
    """If the two `<PORT>` occurrences resolve to DIFFERENT values, that's a
    real inconsistency (hand-edited unit, botched substitution) — not
    something placeholder-tolerance should paper over."""
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-agent.service").write_text(TEMPLATE_UNIT)
    use_packaged(monkeypatch, packaged)
    installed = _agent_installer_installed(tmp_path, machine="dellserver", port="7433")
    installed = installed.replace("(port 7433)", "(port 9999)", 1)
    _install(tmp_path, "coord-agent.service", installed)

    (r,) = ud.probe_unit_drift(make_ctx(tmp_path))
    assert r.severity is Severity.WARN
    assert r.values["matches"] is False


def test_missing_required_flags_still_report_drift(tmp_path, monkeypatch) -> None:
    """elitebook's real #1928 shape: ExecStart drops `--machine`/`--port`
    entirely instead of filling them in — a real defect, and placeholder
    tolerance must not paper over it (acceptance criterion 2)."""
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-agent.service").write_text(TEMPLATE_UNIT)
    use_packaged(monkeypatch, packaged)
    installed = _agent_installer_installed(tmp_path).replace(
        "coord agent --machine dellserver --port 7433",
        "coord agent",
    )
    _install(tmp_path, "coord-agent.service", installed)

    (r,) = ud.probe_unit_drift(make_ctx(tmp_path))
    assert r.severity is Severity.WARN
    assert r.values["matches"] is False
    assert r.values["templated"] is True


def test_templated_unit_warn_never_prints_a_bare_cp_remedy(tmp_path, monkeypatch) -> None:
    """Acceptance criterion 3: followed verbatim, the remedy for a
    still-drifting templated unit must not install unresolved placeholders."""
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    deploy_path = packaged / "coord-agent.service"
    deploy_path.write_text(TEMPLATE_UNIT)
    use_packaged(monkeypatch, packaged)
    # Inconsistent `<PORT>` occurrences (Description says 7433, ExecStart
    # says 9999) — a real defect the placeholder tolerance must not paper
    # over, used here purely to force the WARN path this test inspects.
    broken = _agent_installer_installed(tmp_path).replace("(port 7433)", "(port 9999)", 1)
    installed_path = _install(tmp_path, "coord-agent.service", broken)

    (r,) = ud.probe_unit_drift(make_ctx(tmp_path))
    assert r.severity is Severity.WARN
    assert f"cp {deploy_path} {installed_path}" not in r.detail
    assert "sed" in r.detail
    assert "TEMPLATE" in r.detail
    # The remedy must not be pasted as a `cp` that carries the raw
    # placeholders into the install target.
    assert "<MACHINE_NAME>" not in r.detail.split("sed")[0]


def test_unknown_placeholder_falls_back_to_a_documentation_pointer(tmp_path, monkeypatch) -> None:
    """A placeholder this module has no known safe substitution for must not
    be guessed at — the remedy points at the template's own docs instead."""
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    weird_template = TEMPLATE_UNIT.replace("<MACHINE_NAME>", "<SOME_FUTURE_FIELD>")
    (packaged / "coord-agent.service").write_text(weird_template)
    use_packaged(monkeypatch, packaged)
    _install(tmp_path, "coord-agent.service", "[Unit]\nnot even close\n")

    (r,) = ud.probe_unit_drift(make_ctx(tmp_path))
    assert r.severity is Severity.WARN
    assert "cp " not in r.detail
    assert "SOME_FUTURE_FIELD" in r.detail


def test_non_templated_lane_keeps_the_bare_cp_remedy(tmp_path, monkeypatch) -> None:
    """Only a templated reference changes the remedy — every other lane is
    still `cp`-installed directly from `deploy/`, so the old remedy stays
    correct and unchanged for it."""
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-serve.service").write_text(UNIT_TEXT + "Extra=1\n")
    use_packaged(monkeypatch, packaged)
    _install(tmp_path, "coord-serve.service", UNIT_TEXT)

    (r,) = ud.probe_unit_drift(make_ctx(tmp_path))
    assert r.severity is Severity.WARN
    assert r.values["templated"] is False
    assert "cp" in r.detail and "restart" in r.detail


def test_comment_only_diff_is_not_drift(tmp_path, monkeypatch) -> None:
    """A unit whose only difference from the reference is doc comments —
    e.g. install-agent.sh's PATH comment that `deploy/` doesn't carry — is
    not drift; comments never affect what systemd runs."""
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-serve.service").write_text(UNIT_TEXT)
    use_packaged(monkeypatch, packaged)
    _install(tmp_path, "coord-serve.service", "# an extra comment\n" + UNIT_TEXT)

    (r,) = ud.probe_unit_drift(make_ctx(tmp_path))
    assert r.severity is Severity.OK
    assert r.values["matches"] is True


def test_literal_home_and_percent_h_specifier_are_equivalent(tmp_path, monkeypatch) -> None:
    """install-agent.sh expands `%h` to this host's literal $HOME when it
    writes the unit; `deploy/` keeps `%h` literal for systemd to resolve.
    Both are correct — neither spelling is drift."""
    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-serve.service").write_text(UNIT_TEXT)
    use_packaged(monkeypatch, packaged)
    literal = UNIT_TEXT.replace("%h", str(tmp_path))
    _install(tmp_path, "coord-serve.service", literal)

    (r,) = ud.probe_unit_drift(make_ctx(tmp_path))
    assert r.severity is Severity.OK
    assert r.values["matches"] is True


def test_real_coord_agent_service_matches_a_realistic_install(tmp_path, monkeypatch) -> None:
    """Not synthetic: reads the actual `deploy/coord-agent.service` in this
    checkout and checks it against a realistic install-agent.sh-shaped
    install (real values substituted, `%h` expanded to a literal $HOME, no
    doc header) — the exact real-world shape #1928 was filed against
    (dellserver/precision, which DO carry `--machine`/`--port`). Locks the
    fix to the real file, not just the shrunk `TEMPLATE_UNIT` fixture."""
    real_template_path = Path(__file__).resolve().parents[1] / "deploy" / "coord-agent.service"
    real_template = real_template_path.read_text()
    assert ud._is_templated(real_template), "fixture assumption: the real file is templated"

    packaged = tmp_path / "site-packages" / "coord" / "deploy"
    packaged.mkdir(parents=True)
    (packaged / "coord-agent.service").write_text(real_template)
    use_packaged(monkeypatch, packaged)

    installed_text = (
        real_template
        # install-agent.sh's heredoc keeps only the [Unit]/[Service]/[Install]
        # body and its own PATH comment — the ~76-line doc header above
        # `[Unit]` is dropped entirely.
    )
    # Strip everything before `[Unit]` (the doc header) to mirror
    # install-agent.sh not emitting it, then fill in the placeholders and
    # swap `%h` for a literal $HOME the way the heredoc's `$VENV_DIR`/`$HOME`
    # expansion does.
    body = installed_text[installed_text.index("[Unit]"):]
    body = body.replace("<MACHINE_NAME>", "dellserver").replace("<PORT>", "7433")
    body = body.replace("%h", str(tmp_path))
    _install(tmp_path, "coord-agent.service", body)

    (r,) = ud.probe_unit_drift(make_ctx(tmp_path))
    assert r.severity is Severity.OK, r.detail
    assert r.values["matches"] is True


def test_real_deploy_dir_placeholder_units_never_get_a_bare_cp_remedy() -> None:
    """A repo-wide guard over the actual `deploy/` directory (not a
    synthetic fixture): whatever unit files exist there today or in the
    future, if any carries a `<PLACEHOLDER>`, the WARN path for it must
    never be the bare `cp` — that's #1928's exact failure mode, and this
    pins it against every current and future templated unit, not just
    `coord-agent.service`."""
    import coord

    real_deploy = Path(coord.__file__).resolve().parent / "deploy"
    for deploy_path in ud._unit_files(real_deploy):
        deploy_text = deploy_path.read_text()
        if not ud._is_templated(deploy_text):
            continue
        installed_path = Path("/tmp/nonexistent") / deploy_path.name
        remedy = ud._templated_remedy(
            deploy_text, deploy_path, installed_path, deploy_path.stem
        )
        assert f"cp {deploy_path} {installed_path}" not in remedy
