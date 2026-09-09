"""Unit tests for the `timer_active` machine-scope health check (#2082).

Mirrors `tests/test_health_unit_drift.py`'s structure (same fixtures, same
`make_ctx`/`use_packaged` helpers) since this check reuses that module's
reference resolution outright.

The acceptance-criteria case this file exists to pin down: a timer whose
*content* matches `deploy/` byte for byte, but which `systemctl --user
list-unit-files` reports as `disabled` — the exact dellserver state #2082
found `coord-release-propagate.timer` in after sitting untouched for a day.
`grade_timer_state` must call that CRIT; before this check existed nothing
did, which is the failure this module closes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coord.config import HealthConfig
from coord.health.checks import timer_active as ta
from coord.health.checks import unit_drift as ud
from coord.health.models import Checkout, HealthContext, Severity

NOW = 1_800_000_000.0

TIMER_TEXT = (
    "[Unit]\nDescription=x\n\n[Timer]\nOnUnitActiveSec=20min\n\n"
    "[Install]\nWantedBy=timers.target\n"
)


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


@pytest.fixture(autouse=True)
def no_packaged_units(monkeypatch):
    """Same default as test_health_unit_drift.py: this install ships no
    packaged units unless a test opts in — otherwise the probe would find
    THIS repo's real `coord/deploy/` (#1927)."""
    monkeypatch.setattr(ud, "packaged_unit_dir", lambda: None)


def use_packaged(monkeypatch, path: Path, *, verified: bool = True, version="0.4.110"):
    monkeypatch.setattr(ud, "packaged_unit_dir", lambda: path)
    monkeypatch.setattr(ud, "in_git_worktree", lambda _p: not verified)
    monkeypatch.setattr(ud, "installed_version", lambda: version)


def _make_deploy_dir(tmp_path: Path, units: dict[str, str]) -> Path:
    deploy_dir = tmp_path / "site-packages" / "coord" / "deploy"
    deploy_dir.mkdir(parents=True)
    for name, text in units.items():
        (deploy_dir / name).write_text(text)
    return deploy_dir


# ── grade_timer_state — pure judgement, the core of the check ────────────


def test_disabled_with_a_present_installed_copy_is_crit() -> None:
    """The exact #2082 repro: content matched, `UnitFileState=disabled`."""
    severity, headroom = ta.grade_timer_state(
        {"UnitFileState": "disabled", "UnitFilePreset": "enabled",
         "ActiveState": "inactive", "SubState": "dead"}
    )
    assert severity is Severity.CRIT
    assert "DISABLED" in headroom
    assert "enable --now" in headroom


def test_masked_is_crit() -> None:
    severity, _ = ta.grade_timer_state(
        {"UnitFileState": "masked", "ActiveState": "inactive"}
    )
    assert severity is Severity.CRIT


def test_enabled_but_inactive_is_crit() -> None:
    """Enabled alone is not enough — it must also be running."""
    severity, headroom = ta.grade_timer_state(
        {"UnitFileState": "enabled", "ActiveState": "inactive", "SubState": "dead"}
    )
    assert severity is Severity.CRIT
    assert "not active" in headroom


def test_enabled_and_active_is_ok() -> None:
    severity, headroom = ta.grade_timer_state(
        {"UnitFileState": "enabled", "ActiveState": "active", "SubState": "waiting"}
    )
    assert severity is Severity.OK
    assert "enabled and active" in headroom


def test_static_and_active_is_ok() -> None:
    """A `static` unit (no [Install] of its own — the .service half of a
    timer pair) with no meaningful UnitFileState still reads OK as long as
    it is active; only a genuinely off state is CRIT."""
    severity, _ = ta.grade_timer_state(
        {"UnitFileState": "static", "ActiveState": "active", "SubState": "running"}
    )
    assert severity is Severity.OK


def test_no_fields_at_all_is_unknown_not_ok() -> None:
    severity, headroom = ta.grade_timer_state({})
    assert severity is Severity.UNKNOWN
    assert "no state" in headroom


# ── probe_timer_active — wiring ───────────────────────────────────────────


def test_no_deploy_dir_is_ok(tmp_path) -> None:
    results = ta.probe_timer_active(make_ctx(tmp_path))
    assert len(results) == 1
    assert results[0].severity is Severity.OK
    assert "no deploy/ checkout" in results[0].headroom


def test_no_timer_units_packaged_is_ok(tmp_path, monkeypatch) -> None:
    packaged = _make_deploy_dir(tmp_path, {"coord-serve.service": "[Service]\n"})
    use_packaged(monkeypatch, packaged)
    results = ta.probe_timer_active(make_ctx(tmp_path))
    assert len(results) == 1
    assert results[0].severity is Severity.OK
    assert "no timer units" in results[0].headroom


def test_timer_not_installed_here_is_ok(tmp_path, monkeypatch) -> None:
    packaged = _make_deploy_dir(tmp_path, {"coord-release-propagate.timer": TIMER_TEXT})
    use_packaged(monkeypatch, packaged)
    results = ta.probe_timer_active(make_ctx(tmp_path))
    assert len(results) == 1
    assert results[0].severity is Severity.OK
    assert "no packaged timer is installed" in results[0].headroom


def test_installed_disabled_timer_is_crit(tmp_path, monkeypatch) -> None:
    """The full wiring test for #2082's evidence: a timer installed with
    matching content, reported disabled by systemd, must CRIT — this is
    the test that FAILS against the pre-fix tree (the check did not exist
    at all, so nothing here could ever have caught dellserver's state)."""
    packaged = _make_deploy_dir(tmp_path, {"coord-release-propagate.timer": TIMER_TEXT})
    use_packaged(monkeypatch, packaged)
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    (installed_dir / "coord-release-propagate.timer").write_text(TIMER_TEXT)

    monkeypatch.setattr(
        ta, "_timer_states",
        lambda units: {
            "coord-release-propagate.timer": {
                "Id": "coord-release-propagate.timer",
                "UnitFileState": "disabled",
                "UnitFilePreset": "enabled",
                "ActiveState": "inactive",
                "SubState": "dead",
            }
        },
    )
    results = ta.probe_timer_active(make_ctx(tmp_path))
    assert len(results) == 1
    r = results[0]
    assert r.subject == "coord-release-propagate.timer"
    assert r.severity is Severity.CRIT
    assert "DISABLED" in r.headroom
    assert "enable --now coord-release-propagate.timer" in r.detail


def test_installed_enabled_active_timer_is_ok(tmp_path, monkeypatch) -> None:
    packaged = _make_deploy_dir(tmp_path, {"coord-release-propagate.timer": TIMER_TEXT})
    use_packaged(monkeypatch, packaged)
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    (installed_dir / "coord-release-propagate.timer").write_text(TIMER_TEXT)

    monkeypatch.setattr(
        ta, "_timer_states",
        lambda units: {
            "coord-release-propagate.timer": {
                "Id": "coord-release-propagate.timer",
                "UnitFileState": "enabled",
                "UnitFilePreset": "enabled",
                "ActiveState": "active",
                "SubState": "waiting",
            }
        },
    )
    results = ta.probe_timer_active(make_ctx(tmp_path))
    assert len(results) == 1
    assert results[0].severity is Severity.OK


def test_installed_timer_with_no_systemd_is_unknown(tmp_path, monkeypatch) -> None:
    packaged = _make_deploy_dir(tmp_path, {"coord-release-propagate.timer": TIMER_TEXT})
    use_packaged(monkeypatch, packaged)
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    (installed_dir / "coord-release-propagate.timer").write_text(TIMER_TEXT)

    monkeypatch.setattr(ta, "_timer_states", lambda units: {})
    results = ta.probe_timer_active(make_ctx(tmp_path))
    assert len(results) == 1
    assert results[0].severity is Severity.UNKNOWN


def test_timer_states_parses_a_wedged_systemctl_gracefully(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise FileNotFoundError("systemctl")

    import subprocess

    monkeypatch.setattr(subprocess, "run", _boom)
    assert ta._timer_states(("x.timer",)) == {}


# ── #3219: masked-by-policy sentinel ──────────────────────────────────────


def test_masked_timer_with_no_sentinel_entry_is_not_suppressed(tmp_path, monkeypatch) -> None:
    """#3219: absence of a sentinel entry must not read as "suppressed" —
    the sentinel is the signal, not the masked state itself."""
    packaged = _make_deploy_dir(tmp_path, {"coord-release-propagate.timer": TIMER_TEXT})
    use_packaged(monkeypatch, packaged)
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    (installed_dir / "coord-release-propagate.timer").write_text(TIMER_TEXT)

    monkeypatch.setattr(
        ta, "_timer_states",
        lambda units: {
            "coord-release-propagate.timer": {
                "Id": "coord-release-propagate.timer",
                "UnitFileState": "masked",
                "ActiveState": "inactive",
                "SubState": "dead",
            }
        },
    )
    results = ta.probe_timer_active(make_ctx(tmp_path))
    assert len(results) == 1
    r = results[0]
    assert r.severity is Severity.CRIT
    assert r.values["suppressed"] is False
    assert r.values["suppress_reason"] is None
    assert r.values["suppress_set"] is None


def test_masked_timer_covered_by_the_watchdog_suppress_sentinel_is_flagged(
    tmp_path, monkeypatch
) -> None:
    """#3219 (follow-up to #3049): a timer masked on purpose — covered by
    the same ``watchdog-suppress.json`` sentinel the fleet watchdog and
    `unit_drift` already honour — still reports the honest CRIT (severity
    is this probe's call, not a policy layer's), but surfaces the
    sentinel's reason/set date in ``values`` so a policy-aware consumer
    (`coord release verify`, #3049) can render it as masked-by-policy
    instead of an unclearable fault."""
    packaged = _make_deploy_dir(tmp_path, {"coord-release-propagate.timer": TIMER_TEXT})
    use_packaged(monkeypatch, packaged)
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    (installed_dir / "coord-release-propagate.timer").write_text(TIMER_TEXT)

    monkeypatch.setattr(
        ta, "_timer_states",
        lambda units: {
            "coord-release-propagate.timer": {
                "Id": "coord-release-propagate.timer",
                "UnitFileState": "masked",
                "ActiveState": "inactive",
                "SubState": "dead",
            }
        },
    )

    coord_dir = tmp_path / ".coord"
    coord_dir.mkdir()
    (coord_dir / "watchdog-suppress.json").write_text(
        json.dumps(
            {
                "coord-release-propagate.timer": {
                    "reason": "manual release rolls by choice -- masked, not broken",
                    "set": "2026-08-26",
                    "expires": None,
                }
            }
        )
    )

    results = ta.probe_timer_active(make_ctx(tmp_path, coord_dir=coord_dir))
    assert len(results) == 1
    r = results[0]
    # Still the honest CRIT — this probe has no policy context of its own.
    assert r.severity is Severity.CRIT
    assert r.values["suppressed"] is True
    assert r.values["suppress_reason"] == "manual release rolls by choice -- masked, not broken"
    assert r.values["suppress_set"] == "2026-08-26"


def test_probe_is_registered_in_the_machine_scope_registry() -> None:
    from coord.health.registry import all_checks

    ids = {c.id: c for c in all_checks()}
    assert "timer_active" in ids
    assert ids["timer_active"].scope == "machine"
