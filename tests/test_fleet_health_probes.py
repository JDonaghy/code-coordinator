"""Unit tests for each fleet-scope health probe's own severity logic (#1630).

``tests/test_fleet_health_snapshot.py`` covers the *plumbing* — polling,
persisting, staleness, payload bounding, the advisory-only guard. This module
covers the other half: given a hand-seeded
:class:`~coord.health.models.FleetSnapshot`, does each probe classify severity
the way the milestone's acceptance table says it must?

Everything here drives the probes through ``run_all(ctx, scopes=("fleet",))``
rather than calling the functions directly, so a probe that silently drops out
of the registry (wrong scope, missing ``@check``, an id typo) fails these tests
instead of quietly never running on the daemon.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import HealthConfig
from coord.health.models import FleetSnapshot, HealthContext, Severity
from coord.health.registry import run_all

NOW = 1_800_000_000.0


def _ctx(*, machines: dict | None = None, daemon_host: dict | None = None,
         fleet: bool = True, home: Path | None = None) -> HealthContext:
    ctx = HealthContext(
        thresholds=HealthConfig(),
        home=home or Path("/nonexistent-home"),
        coord_dir=(home or Path("/nonexistent-home")) / ".coord",
        now=NOW,
        allow_network=False,
    )
    if fleet:
        ctx.fleet = FleetSnapshot(
            machines=machines or {}, daemon_host=daemon_host or {}
        )
    return ctx


def _run(ctx: HealthContext) -> dict:
    """check_id -> CheckResult, for every fleet-scope probe in the registry."""
    # Import for the side effect of registering the fleet checks; the registry
    # is populated by module import, so a test module that never imports them
    # would see an empty fleet scope and pass vacuously.
    from coord.health import checks  # noqa: F401, PLC0415

    report = run_all(ctx, scopes=("fleet",))
    return {r.check_id: r for r in report.results}


def _agent(version: str | None, *, errored: bool = False) -> dict:
    """An ``agent_venv`` machine-scope result, shaped like the one the
    refresher builds from a real agent's ``/health`` poll."""
    result = {"check_id": "agent_venv", "values": {"version": version}}
    if errored:
        result["error"] = "pip show exploded"
    return result


def _cli_venv(version: str | None = None, *, present: bool = True,
              errored: bool = False) -> dict:
    """A ``cli_venv`` machine-scope result (#1806) — the fact that used to
    be wrongly gathered by stat-ing the daemon host's own filesystem."""
    result = {"check_id": "cli_venv", "values": {"present": present, "version": version}}
    if errored:
        result["error"] = "pip show exploded"
    return result


def _tui(*, present: bool = True, binary_mtime: float | None = None,
         source_mtime: float | None = None, errored: bool = False) -> dict:
    """A ``tui_binary`` machine-scope result (#1806) — same story as
    ``_cli_venv``: this used to be a single daemon-host ``os.stat``."""
    values: dict = {"present": present, "path": "/home/x/.local/bin/coord-tui"}
    if present:
        values["binary_mtime"] = binary_mtime
        if source_mtime is not None:
            values["source_mtime"] = source_mtime
    result = {"check_id": "tui_binary", "values": values}
    if errored:
        result["error"] = "stat exploded"
    return result


def _unit_drift(subject: str, severity: str, *, installed: bool = True,
                errored: bool = False, reference_verified: bool | None = None) -> dict:
    """A single ``unit_drift`` machine-scope result (#1831) — the
    per-machine-scope check returns one of these per deploy-lane unit.

    ``reference_verified`` (#1927) says whether the machine diffed against
    the units packaged with its installed release or against a working copy
    nothing keeps current. Default None = the key is absent entirely, which
    is what an agent predating #1927 reports.
    """
    result = {
        "check_id": "unit_drift",
        "subject": subject,
        "severity": severity,
        "values": {"installed": installed},
    }
    if reference_verified is not None:
        result["values"]["reference_verified"] = reference_verified
    if errored:
        result["error"] = "probe exploded"
    return result


def _machine(*results: dict, state: str = "online") -> dict:
    """A machine entry shaped like the one the refresher builds, carrying
    whichever machine-scope check results (``_agent``/``_cli_venv``/
    ``_tui``) this test seeds."""
    return {"state": state, "checks": {"results": list(results)}}


# ── every fleet probe is actually registered ─────────────────────────────────


def test_all_fleet_probes_run() -> None:
    results = _run(_ctx())
    assert set(results) == {
        "fleet_deploy_lanes",
        "fleet_tui_binary",
        "fleet_webapp_bundle",
        "fleet_board_latency",
        "fleet_phantom_running",
        "fleet_toolchain_skew",
        "fleet_unit_drift",
        "issues_sync_staleness",
    }


def test_no_fleet_snapshot_means_unknown_never_ok() -> None:
    """`coord health` run by hand on an agent has no fleet view. Every fleet
    probe must read UNKNOWN there — an absent signal is not a passing one."""
    results = _run(_ctx(fleet=False))
    assert results, "fleet probes must still run and report, not be skipped"
    for check_id, r in results.items():
        assert r.severity is Severity.UNKNOWN, check_id
        assert "no fleet snapshot" in r.headroom, check_id


# ── fleet_deploy_lanes ───────────────────────────────────────────────────────
#
# #1806: the ~/.coord-cli-venv lane rides each machine's own `cli_venv`
# machine-scope check now, never a `daemon_host["cli_venv_version"]` key —
# see coord.health.checks.deploy_lane_facts. Every test below seeds it on a
# `machines` entry (usually "elitebook", the operator's machine in the
# issue's live example), never on `daemon_host`.


def test_deploy_lanes_all_agree_is_ok() -> None:
    r = _run(
        _ctx(
            machines={
                "elitebook": _machine(_agent("1.4.0"), _cli_venv("1.4.0")),
                "mini": _machine(_agent("1.4.0")),
            },
            daemon_host={"coord_serve_version": "1.4.0"},
        )
    )["fleet_deploy_lanes"]
    assert r.severity is Severity.OK
    assert "1.4.0" in r.headroom
    assert set(r.values["lanes"]) == {
        "elitebook", "mini", "coord-serve (daemon host)",
        "~/.coord-cli-venv (elitebook)",
    }


def test_deploy_lanes_any_disagreement_is_crit() -> None:
    """One lane behind is the 2026-07-29 incident: the CLI venv three releases
    stale while everyone believed the fix was live."""
    r = _run(
        _ctx(
            machines={
                "elitebook": _machine(_agent("1.4.0"), _cli_venv("1.1.0")),
                "mini": _machine(_agent("1.4.0")),
            },
            daemon_host={"coord_serve_version": "1.4.0"},
        )
    )["fleet_deploy_lanes"]
    assert r.severity is Severity.CRIT
    assert "2 versions" in r.headroom
    # The detail must name *which* lane is the odd one out, not just that skew
    # exists — "something is stale" is not an actionable page.
    assert "~/.coord-cli-venv (elitebook)" in r.detail
    assert "1.1.0" in r.detail


def test_deploy_lanes_agent_skew_is_crit_too() -> None:
    r = _run(
        _ctx(
            machines={
                "elitebook": _machine(_agent("1.4.0"), _cli_venv("1.4.0")),
                "mini": _machine(_agent("1.3.9")),
            },
            daemon_host={"coord_serve_version": "1.4.0"},
        )
    )["fleet_deploy_lanes"]
    assert r.severity is Severity.CRIT
    assert "mini" in r.detail


def test_deploy_lanes_missing_lane_downgrades_agreement_to_unknown() -> None:
    """Agreement among the lanes that *did* answer is not fleet-wide agreement:
    a machine with no data must not read as "matches everyone else"."""
    r = _run(
        _ctx(
            machines={
                "elitebook": _machine(_agent("1.4.0"), _cli_venv("1.4.0")),
                "mini": _machine(_agent(None)),
            },
            daemon_host={"coord_serve_version": "1.4.0"},
        )
    )["fleet_deploy_lanes"]
    assert r.severity is Severity.UNKNOWN
    assert "mini" in r.detail
    assert "1 lane(s) with no data" in r.headroom


def test_deploy_lanes_errored_agent_check_is_no_data_not_a_version() -> None:
    r = _run(
        _ctx(
            machines={
                "elitebook": _machine(_agent("1.4.0", errored=True), _cli_venv("1.4.0")),
            },
            daemon_host={"coord_serve_version": "1.4.0"},
        )
    )["fleet_deploy_lanes"]
    assert r.severity is Severity.UNKNOWN
    assert r.values["lanes"]["elitebook"] is None


def test_deploy_lanes_no_lane_has_data_is_unknown() -> None:
    r = _run(
        _ctx(
            machines={"elitebook": _machine(_agent(None))},
            daemon_host={"coord_serve_version": None},
        )
    )["fleet_deploy_lanes"]
    assert r.severity is Severity.UNKNOWN
    assert "no lane has a resolvable version" in r.headroom
    # No machine reported a CLI venv at all — the lane is a single "no data"
    # entry, not silently dropped from the report.
    assert r.values["lanes"]["~/.coord-cli-venv"] is None


def test_deploy_lanes_cli_venv_present_on_a_non_daemon_machine_is_read() -> None:
    """#1806 acceptance: the venv lives on exactly one non-daemon machine —
    the lane must resolve from there, named by that machine."""
    r = _run(
        _ctx(
            machines={
                "elitebook": _machine(_agent("1.4.0"), _cli_venv("1.4.0")),
                "dellserver": _machine(_agent("1.4.0")),
            },
            daemon_host={"coord_serve_version": "1.4.0"},
        )
    )["fleet_deploy_lanes"]
    assert r.severity is Severity.OK
    assert r.values["lanes"]["~/.coord-cli-venv (elitebook)"] == "1.4.0"
    assert "~/.coord-cli-venv (dellserver)" not in r.values["lanes"]


def test_deploy_lanes_ignores_a_stray_daemon_host_cli_venv_version_key() -> None:
    """#1806 regression: even if something still writes a stray
    `daemon_host["cli_venv_version"]` (an old daemon build, a leftover test
    fixture), the lane must come from the machine-scope `cli_venv` check,
    never from that key."""
    r = _run(
        _ctx(
            machines={"elitebook": _machine(_agent("1.4.0"), _cli_venv("9.9.9"))},
            daemon_host={"coord_serve_version": "1.4.0", "cli_venv_version": "0.0.1"},
        )
    )["fleet_deploy_lanes"]
    assert r.values["lanes"]["~/.coord-cli-venv (elitebook)"] == "9.9.9"
    assert "0.0.1" not in r.values["lanes"].values()


# ── fleet_tui_binary ─────────────────────────────────────────────────────────
#
# #1806: same story as the CLI-venv lane above — every fact rides a
# `machines` entry's own `tui_binary` check now, never `daemon_host`.


def test_tui_binary_newer_than_source_is_ok() -> None:
    r = _run(
        _ctx(
            machines={
                "elitebook": _machine(_tui(binary_mtime=NOW, source_mtime=NOW - 3600)),
            }
        )
    )["fleet_tui_binary"]
    assert r.severity is Severity.OK
    assert "up to date" in r.headroom
    assert "elitebook" in r.headroom


def test_tui_binary_older_than_source_is_warn_with_the_staleness_in_hours() -> None:
    r = _run(
        _ctx(
            machines={
                "elitebook": _machine(
                    _tui(binary_mtime=NOW - 9000, source_mtime=NOW)  # 2.5h stale
                ),
            }
        )
    )["fleet_tui_binary"]
    assert r.severity is Severity.WARN
    assert "2.5h older" in r.headroom
    assert "elitebook" in r.headroom
    assert "rebuild" in r.detail
    assert "elitebook" in r.detail


def test_tui_binary_exactly_equal_mtimes_is_ok_not_warn() -> None:
    """Boundary: `cp` preserving mtime must not read as stale forever."""
    r = _run(
        _ctx(machines={"elitebook": _machine(_tui(binary_mtime=NOW, source_mtime=NOW))})
    )["fleet_tui_binary"]
    assert r.severity is Severity.OK


def test_tui_binary_no_machine_reports_one_is_unknown_and_says_how_to_build_it() -> None:
    r = _run(
        _ctx(machines={"elitebook": _machine(_tui(present=False))})
    )["fleet_tui_binary"]
    assert r.severity is Severity.UNKNOWN
    assert "no machine reports a coord-tui binary" in r.headroom
    assert "cargo build" in r.detail


def test_tui_binary_present_but_no_source_tree_is_ok_not_a_fabricated_verdict() -> None:
    r = _run(
        _ctx(machines={"elitebook": _machine(_tui(binary_mtime=NOW))})
    )["fleet_tui_binary"]
    assert r.severity is Severity.OK
    assert "no source tree found to compare" in r.headroom


def test_tui_binary_operator_fresh_and_non_operator_absent_is_ok_names_no_machine() -> None:
    """#1806 acceptance, half one: the operator's (elitebook) binary is
    fresh; a non-operator machine (dellserver, the daemon host in the
    issue's live example) reports no coord-tui at all. Absence on a machine
    that was never meant to have this lane must not taint the verdict."""
    r = _run(
        _ctx(
            machines={
                "elitebook": _machine(_tui(binary_mtime=NOW, source_mtime=NOW - 3600)),
                "dellserver": _machine(_tui(present=False)),
            }
        )
    )["fleet_tui_binary"]
    assert r.severity is Severity.OK
    assert "dellserver" not in r.headroom
    assert "dellserver" not in r.values.get("stale", [])


def test_tui_binary_operator_stale_and_non_operator_fresh_is_warn_naming_the_operator() -> None:
    """#1806 acceptance, half two: flip it — the machine that's actually
    stale (elitebook, the operator) must be the one named in WARN, even
    though another machine (dellserver) reports a perfectly fresh binary.
    This is the exact symmetric failure the issue calls out: the daemon-host
    -only version of this check could never see elitebook go stale."""
    r = _run(
        _ctx(
            machines={
                "elitebook": _machine(
                    _tui(binary_mtime=NOW - 9000, source_mtime=NOW)  # stale
                ),
                "dellserver": _machine(_tui(binary_mtime=NOW, source_mtime=NOW - 3600)),
            }
        )
    )["fleet_tui_binary"]
    assert r.severity is Severity.WARN
    assert "elitebook" in r.headroom
    assert "dellserver" not in r.headroom
    assert r.values["stale"] == ["elitebook"]


def test_tui_binary_errored_machine_check_is_excluded_not_treated_as_present() -> None:
    r = _run(
        _ctx(machines={"elitebook": _machine(_tui(binary_mtime=NOW, errored=True))})
    )["fleet_tui_binary"]
    assert r.severity is Severity.UNKNOWN
    assert "no machine reports a coord-tui binary" in r.headroom


# ── fleet_webapp_bundle (#1834 lane 5) ────────────────────────────────────────
#
# Same shape as fleet_tui_binary above — a `webapp_bundle` machine-scope fact,
# aggregated across `ctx.fleet.machines`, judged for staleness against its own
# source tree, never for version (see deploy_lane_facts.py's module
# docstring for why: the bundle is SHA-versioned off a continuous publish
# timer, not pip-versioned like every other lane in this module).


def _webapp(*, present: bool = True, sha: str | None = None,
            dist_mtime: float | None = None, source_mtime: float | None = None,
            errored: bool = False) -> dict:
    values: dict = {"present": present, "path": "/home/x/coord-web-dist"}
    if present:
        values["dist_mtime"] = dist_mtime
        if sha is not None:
            values["sha"] = sha
        if source_mtime is not None:
            values["source_mtime"] = source_mtime
    result = {"check_id": "webapp_bundle", "values": values}
    if errored:
        result["error"] = "stat exploded"
    return result


def test_webapp_bundle_newer_than_source_is_ok() -> None:
    r = _run(
        _ctx(
            machines={
                "dellserver": _machine(
                    _webapp(dist_mtime=NOW, source_mtime=NOW - 3600)
                ),
            }
        )
    )["fleet_webapp_bundle"]
    assert r.severity is Severity.OK
    assert "up to date" in r.headroom
    assert "dellserver" in r.headroom


def test_webapp_bundle_older_than_source_is_warn_with_the_staleness_in_hours() -> None:
    r = _run(
        _ctx(
            machines={
                "dellserver": _machine(
                    _webapp(dist_mtime=NOW - 9000, source_mtime=NOW)  # 2.5h stale
                ),
            }
        )
    )["fleet_webapp_bundle"]
    assert r.severity is Severity.WARN
    assert "2.5h older" in r.headroom
    assert "dellserver" in r.headroom
    assert "coord-web-dist-build.timer" in r.detail
    assert "dellserver" in r.detail


def test_webapp_bundle_no_machine_reports_one_is_unknown() -> None:
    r = _run(
        _ctx(machines={"dellserver": _machine(_webapp(present=False))})
    )["fleet_webapp_bundle"]
    assert r.severity is Severity.UNKNOWN
    assert "no machine reports a coord-web-dist bundle" in r.headroom
    assert "coord-web-dist-build" in r.detail


def test_webapp_bundle_present_but_no_source_tree_is_ok_not_a_fabricated_verdict() -> None:
    r = _run(
        _ctx(machines={"dellserver": _machine(_webapp(dist_mtime=NOW))})
    )["fleet_webapp_bundle"]
    assert r.severity is Severity.OK
    assert "no source tree found to compare" in r.headroom


def test_webapp_bundle_two_machines_serving_different_shas_is_warn() -> None:
    """This is drift too — no comparable source, but two machines both
    running `coord web` disagree on which build is live."""
    r = _run(
        _ctx(
            machines={
                "dellserver": _machine(_webapp(sha="aaa111", dist_mtime=NOW)),
                "elitebook": _machine(_webapp(sha="bbb222", dist_mtime=NOW)),
            }
        )
    )["fleet_webapp_bundle"]
    assert r.severity is Severity.WARN
    assert "2 different bundles" in r.headroom
    assert "aaa111" in r.detail and "bbb222" in r.detail


def test_webapp_bundle_matching_shas_across_machines_is_ok() -> None:
    r = _run(
        _ctx(
            machines={
                "dellserver": _machine(_webapp(sha="aaa111", dist_mtime=NOW)),
                "elitebook": _machine(_webapp(sha="aaa111", dist_mtime=NOW)),
            }
        )
    )["fleet_webapp_bundle"]
    assert r.severity is Severity.OK


def test_webapp_bundle_errored_machine_check_is_excluded_not_treated_as_present() -> None:
    r = _run(
        _ctx(machines={"dellserver": _machine(_webapp(dist_mtime=NOW, errored=True))})
    )["fleet_webapp_bundle"]
    assert r.severity is Severity.UNKNOWN
    assert "no machine reports a coord-web-dist bundle" in r.headroom


# ── fleet_board_latency ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload_bytes", "expected"),
    [
        (1024, Severity.OK),
        (2 * 1024 * 1024 - 1, Severity.OK),
        (2 * 1024 * 1024, Severity.WARN),  # boundary: >= warns
        (4 * 1024 * 1024, Severity.WARN),
        (5 * 1024 * 1024, Severity.CRIT),  # boundary: >= crits
        (6 * 1024 * 1024, Severity.CRIT),  # the #1336 5.3MB payload's class
    ],
)
def test_board_payload_size_thresholds(payload_bytes, expected) -> None:
    r = _run(
        _ctx(daemon_host={"board_latency_ms": 5.0, "board_payload_bytes": payload_bytes})
    )["fleet_board_latency"]
    assert r.severity is expected


@pytest.mark.parametrize(
    ("latency_ms", "expected"),
    [
        (10.0, Severity.OK),
        (1499.0, Severity.OK),
        (1500.0, Severity.WARN),
        (3999.0, Severity.WARN),
        (4000.0, Severity.CRIT),
    ],
)
def test_board_latency_thresholds(latency_ms, expected) -> None:
    r = _run(
        _ctx(daemon_host={"board_latency_ms": latency_ms, "board_payload_bytes": 1024})
    )["fleet_board_latency"]
    assert r.severity is expected


def test_board_crit_payload_is_not_downgraded_by_a_merely_warn_latency() -> None:
    """Ordering trap: latency is judged after size, so a WARN-level latency
    must not overwrite a CRIT already set by the payload."""
    r = _run(
        _ctx(
            daemon_host={
                "board_latency_ms": 1600.0,  # WARN band
                "board_payload_bytes": 6 * 1024 * 1024,  # CRIT band
            }
        )
    )["fleet_board_latency"]
    assert r.severity is Severity.CRIT


def test_board_never_measured_is_unknown() -> None:
    r = _run(_ctx(daemon_host={}))["fleet_board_latency"]
    assert r.severity is Severity.UNKNOWN
    assert "no /board build measured yet" in r.headroom


def test_board_one_measurement_present_is_still_judged() -> None:
    """A daemon that recorded size but not latency (or vice versa) must be
    judged on what it has rather than falling back to "no data"."""
    r = _run(
        _ctx(daemon_host={"board_latency_ms": None, "board_payload_bytes": 6 * 1024 * 1024})
    )["fleet_board_latency"]
    assert r.severity is Severity.CRIT


# ── fleet_phantom_running ────────────────────────────────────────────────────


def test_phantom_zero_rows_is_ok() -> None:
    r = _run(_ctx(daemon_host={"phantom_running": []}))["fleet_phantom_running"]
    assert r.severity is Severity.OK
    assert "0 phantom" in r.headroom


def test_phantom_no_scan_yet_is_unknown_not_ok() -> None:
    """An empty list means "scanned, found none"; a missing key means "never
    scanned". Collapsing the second into the first is #1485's failure mode."""
    r = _run(_ctx(daemon_host={}))["fleet_phantom_running"]
    assert r.severity is Severity.UNKNOWN
    assert "no phantom-row scan yet" in r.headroom


def test_phantom_single_row_is_crit_and_named() -> None:
    r = _run(
        _ctx(
            daemon_host={
                "phantom_running": [
                    {"repo_name": "api", "issue_number": 42,
                     "machine": "mini", "assignment_id": "a1"}
                ]
            }
        )
    )["fleet_phantom_running"]
    assert r.severity is Severity.CRIT
    assert r.headroom == "1 phantom running row"  # singular
    assert "api#42@mini" in r.detail
    assert r.values["count"] == 1
    assert r.values["assignment_ids"] == ["a1"]


def test_phantom_many_rows_samples_five_and_says_there_are_more() -> None:
    rows = [
        {"repo_name": "api", "issue_number": n, "machine": "mini",
         "assignment_id": f"a{n}"}
        for n in range(8)
    ]
    r = _run(_ctx(daemon_host={"phantom_running": rows}))["fleet_phantom_running"]
    assert r.severity is Severity.CRIT
    assert r.headroom == "8 phantom running rows"  # plural
    assert r.detail.endswith(", ...")
    assert r.detail.count("api#") == 5
    # No silent cap: every id is still carried in `values` for a machine
    # consumer even though the human-facing detail samples five.
    assert len(r.values["assignment_ids"]) == 8



# ── fleet_unit_drift ─────────────────────────────────────────────────────────
#
# #1831: aggregates every machine's own `unit_drift` machine-scope check
# (coord.health.checks.unit_drift) the same way fleet_deploy_lanes aggregates
# cli_venv/tui_binary — see that section's header for the shared rationale.


def test_unit_drift_no_data_anywhere_is_unknown() -> None:
    r = _run(_ctx(machines={"dellserver": _machine()}))["fleet_unit_drift"]
    assert r.severity is Severity.UNKNOWN
    assert "no machine has reported" in r.headroom


def test_unit_drift_all_matching_is_ok() -> None:
    r = _run(
        _ctx(
            machines={
                "dellserver": _machine(
                    _unit_drift("coord-serve.service", "ok"),
                    _unit_drift("coord-agent.service", "ok"),
                ),
                "elitebook": _machine(
                    _unit_drift("coord-agent.service", "ok", installed=False),
                ),
            }
        )
    )["fleet_unit_drift"]
    assert r.severity is Severity.OK
    assert r.values["checked_units"] == 2  # only the installed=True ones


def test_unit_drift_stale_unit_is_warn_and_names_the_machine_and_unit() -> None:
    """The acceptance-criteria "stale unit -> reported" half, at fleet scope."""
    r = _run(
        _ctx(
            machines={
                "dellserver": _machine(
                    _unit_drift("coord-serve.service", "warn"),
                )
            }
        )
    )["fleet_unit_drift"]
    assert r.severity is Severity.WARN
    assert "dellserver/coord-serve.service" in r.headroom
    assert r.values["stale"] == [{"machine": "dellserver", "unit": "coord-serve.service"}]


def test_unit_drift_matching_unit_is_ok_and_silent() -> None:
    """The acceptance-criteria "matching unit -> silent" half, at fleet scope."""
    r = _run(
        _ctx(
            machines={
                "dellserver": _machine(
                    _unit_drift("coord-serve.service", "ok"),
                )
            }
        )
    )["fleet_unit_drift"]
    assert r.severity is Severity.OK
    assert r.values["stale"] == []
    assert r.values["shadowed"] == []


def test_unit_drift_unverified_reference_is_unknown_not_ok() -> None:
    """#1927: a machine that diffed against its own git checkout proved
    nothing — both sides go stale together — so its match must not count
    toward the fleet's green."""
    r = _run(
        _ctx(
            machines={
                "dellserver": _machine(
                    _unit_drift(
                        "coord-serve.service", "unknown", reference_verified=False
                    ),
                ),
                "elitebook": _machine(
                    _unit_drift(
                        "coord-agent.service", "ok", reference_verified=True
                    ),
                ),
            }
        )
    )["fleet_unit_drift"]
    assert r.severity is Severity.UNKNOWN
    assert "dellserver/coord-serve.service" in r.headroom
    assert "elitebook" not in r.headroom
    assert r.values["unverified_reference"] == [
        {"machine": "dellserver", "unit": "coord-serve.service"}
    ]


def test_unit_drift_verified_reference_still_reads_ok() -> None:
    r = _run(
        _ctx(
            machines={
                "dellserver": _machine(
                    _unit_drift("coord-serve.service", "ok", reference_verified=True),
                )
            }
        )
    )["fleet_unit_drift"]
    assert r.severity is Severity.OK
    assert r.values["unverified_reference"] == []


def test_unit_drift_real_drift_outranks_an_unverified_reference() -> None:
    """A machine that can't vouch for its reference must never mask a
    machine that found actual drift."""
    r = _run(
        _ctx(
            machines={
                "dellserver": _machine(
                    _unit_drift(
                        "coord-serve.service", "unknown", reference_verified=False
                    ),
                ),
                "elitebook": _machine(
                    _unit_drift("coord-agent.service", "warn", reference_verified=True),
                ),
            }
        )
    )["fleet_unit_drift"]
    assert r.severity is Severity.WARN
    assert "elitebook/coord-agent.service" in r.headroom


def test_unit_drift_path_shadow_risk_is_crit_and_beats_a_merely_stale_unit() -> None:
    r = _run(
        _ctx(
            machines={
                "dellserver": _machine(
                    _unit_drift("coord-serve.service", "crit"),
                ),
                "elitebook": _machine(
                    _unit_drift("coord-agent.service", "warn"),
                ),
            }
        )
    )["fleet_unit_drift"]
    assert r.severity is Severity.CRIT
    assert "dellserver/coord-serve.service" in r.headroom
    assert r.values["shadowed"] == [
        {"machine": "dellserver", "unit": "coord-serve.service"}
    ]


def test_unit_drift_errored_machine_check_is_excluded_not_treated_as_present() -> None:
    r = _run(
        _ctx(
            machines={
                "dellserver": _machine(
                    _unit_drift("coord-serve.service", "unknown", errored=True),
                )
            }
        )
    )["fleet_unit_drift"]
    assert r.severity is Severity.UNKNOWN
    assert "no machine has reported" in r.headroom
