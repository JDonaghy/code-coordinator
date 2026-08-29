"""#2915: ``coord machine add`` / ``coord machine doctor`` — the machine-side
analogue of #2220.

The bar these tests hold is the one #2220 set for repos: **one distinct, NAMED
finding per defect**, produced from a seeded half-onboarded fleet with no
network and no live agents. A verifier that collapses six different failures
into a generic "machine looks unhealthy" line is worth no more than the runbook
it replaces.

Each of the six silent failures that onboarding ``dell64`` cost on 2026-08-28
gets a test here, referenced by the check id it produces:

1. partial ``~/.coord-venv``      → ``runtime.agent_venv_broken``
2. ``host:`` resolves to LAN      → ``network.host_resolves_offtailnet``
                                    (and ``coord machine add`` REFUSING it)
3. agent came up config-free      → ``agent.config_free``
4. ``repo_paths`` key = dirname   → ``coord machine add`` refusing the write,
                                    plus ``config.repo_path_missing``
5. live config replaced by a file → out of scope (fleet_config_health owns it)
6. ``graphify`` absent            → ``graph.graphify_missing``
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import machine_onboard
from coord.commands.machine import machine_add, machine_doctor
from coord.config import ConfigError
from coord.config import load as load_config
from coord.machine_onboard import CRIT, OK, UNKNOWN, WARN, MachineFacts
from coord.network import ONLINE, TIMEOUT, MachineStatus


CONFIG = """\
repos:
  - name: api
    github: acme/api
    depends_on: []
    default_branch: main
    test_command: "make test"

  - name: web
    github: acme/web
    depends_on: []
    default_branch: main

machines:
  - name: laptop
    host: laptop.tail1234.ts.net
    capabilities: [python]
    repos: [api]
    repo_paths:
      api: ~/src/api
"""


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG)
    return p


@pytest.fixture
def cfg(config_path):
    return load_config(config_path)


def _status(machine, *, online=True, health=None):
    return MachineStatus(
        machine=machine,
        state=ONLINE if online else TIMEOUT,
        reason="" if online else "timed out",
        health=health if online else None,
    )


def _health(**overrides):
    """A fully-onboarded agent's ``/health`` body. Tests break one thing."""
    body = {
        "machine": "laptop",
        "capabilities": ["python"],
        "config_free": None,
        "repos": ["api"],
        "degraded": {},
        "version": "0.9.0",
        "tool_versions": {
            "git": {"capability": None, "found": True, "version": "2.40.0"},
            "python3": {"capability": "python", "found": True, "version": "3.12.1"},
        },
        "health": {
            "results": [
                {"check_id": "agent_venv", "severity": "ok",
                 "headroom": "code-coordinator 0.9.0 (non-editable)", "subject": None},
                {"check_id": "graphify_cli", "severity": "ok",
                 "headroom": "graphify 1.2.3", "subject": None},
                {"check_id": "graph", "severity": "ok",
                 "headroom": "0.4h fresh", "subject": "api"},
            ]
        },
    }
    body.update(overrides)
    return body


def _facts(cfg, *, health=None, online=True, ts_matches=True, **kw):
    machine = cfg.machines[0]
    statuses = [_status(machine, online=online, health=health or _health())]
    ts_map = {"laptop": ("100.64.0.5", "laptop.tail1234.ts.net")}
    with patch("coord.network.resolve_host_ip",
               return_value="100.64.0.5" if ts_matches else "192.168.1.44"):
        return machine_onboard.gather_facts(
            cfg, "laptop", statuses=statuses, ts_map=ts_map, **kw
        )


def _checks(report):
    return {f.check for f in report.findings}


def _by_check(report, check):
    return next(f for f in report.findings if f.check == check)


# ── The happy path: a fully-onboarded machine is genuinely clean ────────────


def test_fully_onboarded_machine_is_ok(cfg):
    """The baseline that makes every other test meaningful: a machine with
    nothing wrong must produce zero CRITs, or the findings below are just
    noise that happens to fire."""
    report = machine_onboard.evaluate(_facts(cfg))
    assert report.crits == [], [f.check for f in report.crits]
    assert report.ok
    assert "MACHINE_DOCTOR: machine=laptop crit=0" in machine_onboard.summary_line(report)


def test_linger_is_unknown_not_ok_without_the_ssh_probe(cfg):
    """`/health` structurally cannot see systemd linger — an agent answering
    a probe only proves the user manager is up RIGHT NOW. Reporting that as a
    pass would be the exact "silent" failure this feature exists to end."""
    report = machine_onboard.evaluate(_facts(cfg))
    finding = _by_check(report, "runtime.linger_unknown")
    assert finding.severity == UNKNOWN
    assert "--ssh" in finding.summary


# ── Incident 1: a partial ~/.coord-venv poisoned every retry ────────────────


def test_broken_agent_venv_crits(cfg):
    health = _health(health={"results": [
        {"check_id": "agent_venv", "severity": "crit",
         "headroom": "EDITABLE install detected", "subject": None,
         "detail": "points at /home/john/src/claude-coordinator"},
    ]})
    report = machine_onboard.evaluate(_facts(cfg, health=health))
    finding = _by_check(report, "runtime.agent_venv_broken")
    assert finding.severity == CRIT
    assert "EDITABLE install detected" in finding.summary
    # The remedy must name the trap, not just say "reinstall".
    assert "partial" in (finding.fix or "").lower()


# ── Incident 2: host: resolved to a LAN device, not the tailnet node ────────


def test_host_resolving_off_tailnet_crits_even_though_agent_is_healthy(cfg):
    """The finding that cost an afternoon: the agent is perfectly healthy and
    the board reads [timeout], because `host:` names a LAN device."""
    report = machine_onboard.evaluate(_facts(cfg, ts_matches=False))
    finding = _by_check(report, "network.host_resolves_offtailnet")
    assert finding.severity == CRIT
    assert "192.168.1.44" in finding.summary
    # ... and it fires alongside a REACHABLE agent, which is the whole point.
    assert _by_check(report, "network.agent_reachable").severity == OK
    assert "laptop.tail1234.ts.net" in (finding.fix or "")


def test_host_resolution_without_tailscale_data_is_unknown_not_a_defect(cfg):
    """Absence of evidence is not evidence: a box with no local tailscale must
    stay silent rather than fabricate a mismatch."""
    machine = cfg.machines[0]
    facts = machine_onboard.gather_facts(
        cfg, "laptop", statuses=[_status(machine, health=_health())], ts_map={}
    )
    report = machine_onboard.evaluate(facts)
    assert "network.host_resolution_unknown" in _checks(report)
    assert "network.host_resolves_offtailnet" not in _checks(report)
    assert report.ok


# ── Incident 3: the agent came up config-free ───────────────────────────────


def test_config_free_agent_with_a_standing_config_entry_warns(cfg):
    health = _health(config_free="no local coordinator.yml", capabilities=[], repos=[])
    report = machine_onboard.evaluate(_facts(cfg, health=health))
    assert _by_check(report, "agent.config_free").severity == WARN
    # It must NOT also fire the #1712 "declares caps, publishes none" CRIT —
    # empty publication is the DESIGNED shape for a config-free agent (#1801).
    assert "agent.capabilities_unpublished" not in _checks(report)
    # And clone presence becomes unverifiable, not clean.
    assert _by_check(report, "clones.unverifiable_config_free").severity == UNKNOWN


def test_configured_agent_publishing_no_capabilities_crits(cfg):
    """The other half of #1712: NOT config-free, so empty publication is a
    real misconfiguration rather than a designed absence."""
    health = _health(capabilities=[], repos=[])
    report = machine_onboard.evaluate(_facts(cfg, health=health))
    assert _by_check(report, "agent.capabilities_unpublished").severity == CRIT
    assert _by_check(report, "agent.repos_unpublished").severity == CRIT


# ── Incident 4: repo_paths keyed by directory name, not repo name ───────────


def test_declared_repo_without_a_repo_path_crits():
    """The survivable half of incident 4 (#1801): the config LOADS, and every
    dispatch of that repo to that machine is refused."""
    facts = MachineFacts(
        name="laptop", configured=True, host="laptop.tail1234.ts.net",
        declared_capabilities=["python"], declared_repos=["api", "web"],
        repo_paths={"api": "~/src/api"}, known_repos=["api", "web"],
    )
    report = machine_onboard.evaluate(facts)
    finding = _by_check(report, "config.repo_path_missing")
    assert finding.severity == CRIT
    assert "web" in finding.summary
    # The remedy has to teach the key-vs-value distinction, since getting it
    # wrong takes the whole fleet's config down.
    assert "KEY" in (finding.fix or "")


def test_a_machine_declaring_no_repos_is_told_the_vocabulary_to_use():
    """A brand-new machine that looks perfect and is never routed anything.
    The remedy names the fleet's actual repo names, because guessing them is
    exactly how the directory-name-as-key mistake gets made."""
    facts = MachineFacts(
        name="dell64", configured=True, host="dell64.tail1234.ts.net",
        declared_capabilities=["python"], known_repos=["api", "web"],
    )
    finding = _by_check(machine_onboard.evaluate(facts), "config.no_repos")
    assert finding.severity == CRIT
    assert "['api', 'web']" in finding.summary
    assert "not checkout directory names" in (finding.fix or "")


def test_config_loader_names_the_key_vs_value_trap(tmp_path):
    """Incident 4's fatal half: a `repo_paths` KEY that names no configured
    repo aborts the ENTIRE load — for every machine. The message has to say
    so, or an operator reads it as a problem with one machine."""
    bad = tmp_path / "coordinator.yml"
    bad.write_text(CONFIG.replace("      api: ~/src/api", "      acme-api: ~/src/api"))
    with pytest.raises(ConfigError) as excinfo:
        load_config(bad)
    message = str(excinfo.value)
    assert "acme-api" in message
    assert "['api', 'web']" in message  # the vocabulary it must be drawn from
    assert "ENTIRE config load" in message


# ── Incident 6: graphify absent, so every graph query degrades to grep ──────


def test_missing_graphify_cli_crits(cfg):
    health = _health(health={"results": [
        {"check_id": "graphify_cli", "severity": "warn",
         "headroom": "not installed", "subject": None},
    ]})
    report = machine_onboard.evaluate(_facts(cfg, health=health))
    finding = _by_check(report, "graph.graphify_missing")
    assert finding.severity == CRIT
    assert "SILENTLY" in finding.summary


def test_stale_checkout_graph_warns_but_does_not_crit(cfg):
    """A stale graph on one machine is a state the fleet runs in deliberately
    while the agent self-heals — WARN, so it never turns the gate red."""
    health = _health(health={"results": [
        {"check_id": "graphify_cli", "severity": "ok", "headroom": "graphify 1.2.3"},
        {"check_id": "graph", "severity": "warn",
         "headroom": "128.8h stale, hooks disabled", "subject": "api"},
    ]})
    report = machine_onboard.evaluate(_facts(cfg, health=health))
    assert _by_check(report, "graph.checkout_degraded").severity == WARN
    assert report.ok


# ── Clones: the worker worktree base ───────────────────────────────────────


def test_missing_clone_crits_with_the_configured_path(cfg):
    health = _health(repos=[], degraded={"api": "repo_path ~/src/api does not exist"})
    report = machine_onboard.evaluate(_facts(cfg, health=health))
    finding = _by_check(report, "clones.missing")
    assert finding.severity == CRIT
    assert "~/src/api" in (finding.fix or "")


def test_repo_served_by_config_but_not_by_the_agent_names_the_2219_skew(cfg):
    """Declared, not degraded, not served: the agent's own config predates the
    entry. The remedy is a `git pull` on that host, NOT a restart."""
    health = _health(repos=[], degraded={})
    report = machine_onboard.evaluate(_facts(cfg, health=health))
    finding = _by_check(report, "clones.not_served")
    assert finding.severity == CRIT
    assert "no restart" in (finding.fix or "").lower()


# ── Version vs fleet ───────────────────────────────────────────────────────


def test_version_skew_against_the_fleet_majority_warns(cfg):
    facts = _facts(cfg)
    facts.fleet_versions = {"laptop": "0.9.0", "a": "0.9.1", "b": "0.9.1"}
    report = machine_onboard.evaluate(facts)
    finding = _by_check(report, "agent.version_skew")
    assert finding.severity == WARN
    assert "0.9.1" in finding.summary


def test_a_tied_fleet_has_no_majority_to_grade_against(cfg):
    """Grading a machine against an arbitrary tie-break would report a coin
    flip as a defect."""
    assert machine_onboard.fleet_version_mode({"a": "1", "b": "2"}) is None
    assert machine_onboard.fleet_version_mode({"a": "1"}) is None
    assert machine_onboard.fleet_version_mode({"a": "1", "b": "1", "c": "2"}) == "1"


# ── systemd linger: the one thing /health structurally cannot see ──────────


def _ssh_result(stdout, returncode=0, stderr=""):
    import subprocess  # noqa: PLC0415

    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("Linger=yes\n", True), ("Linger=no\n", False)],
)
def test_probe_linger_parses_loginctl(stdout, expected):
    with patch("subprocess.run", return_value=_ssh_result(stdout)):
        assert machine_onboard.probe_linger("host") == (expected, None)


def test_probe_linger_fails_soft_rather_than_reporting_a_defect():
    """A machine with no `loginctl` at all (macOS uses launchd —
    docs/MAC_MINI.md) must read UNKNOWN, never "linger disabled"."""
    with patch("subprocess.run", return_value=_ssh_result("Linger=unknown\n")):
        linger, error = machine_onboard.probe_linger("host")
    assert linger is None
    assert "not systemd" in error

    with patch("subprocess.run",
               return_value=_ssh_result("", returncode=255, stderr="No route to host")):
        linger, error = machine_onboard.probe_linger("host")
    assert linger is None
    assert "No route to host" in error


def test_disabled_linger_crits_and_names_the_delayed_symptom():
    facts = MachineFacts(
        name="laptop", configured=True, host="laptop.tail1234.ts.net",
        declared_capabilities=["python"], declared_repos=["api"],
        repo_paths={"api": "~/src/api"}, known_repos=["api"],
        linger=False,
    )
    finding = _by_check(machine_onboard.evaluate(facts), "runtime.linger_disabled")
    assert finding.severity == CRIT
    # The symptom is DELAYED — that is what makes it silent.
    assert "next logout or reboot" in finding.summary
    assert "enable-linger" in (finding.fix or "")


# ── The doctor fold-in must not duplicate what `coord doctor` already prints ─


def test_doctor_summary_lines_only_carry_the_layers_doctor_does_not_render(cfg):
    """`coord doctor` already prints the #2912 host line, its own unreachable
    line, the #1712 cross-check and the #1570 D probes. Folding those in again
    would print each finding twice under two names.

    ``clones`` is one of those already-covered layers: the #1712 cross-check
    (``_health_vs_config_lines``) derives "declared repo, not served" from
    the same ``degraded``/``published_repos`` shape ``evaluate_clones`` reads,
    just under its own ``CRIT repos: ...`` name — so it must NOT come through
    ``doctor_summary_lines``'s default layers either (a regression a review
    of #2915 caught: both fired for the same defect in the same `coord
    doctor` run)."""
    health = _health(
        repos=[], degraded={"api": "repo_path missing"},
        capabilities=[],
        health={"results": [
            {"check_id": "graphify_cli", "severity": "warn", "headroom": "not installed"},
            {"check_id": "agent_venv", "severity": "crit", "headroom": "partial venv"},
        ]},
    )
    report = machine_onboard.evaluate(_facts(cfg, health=health, ts_matches=False))
    lines = "\n".join(line for _, line in machine_onboard.doctor_summary_lines(report))
    assert "graph.graphify_missing" in lines
    assert "runtime.agent_venv_broken" in lines
    # Already rendered by `coord doctor` itself — must not appear twice.
    assert "host_resolves_offtailnet" not in lines
    assert "capabilities_unpublished" not in lines
    assert "clones.missing" not in lines
    # The full `clones` layer is still available directly, for `coord
    # machine doctor`'s own report — only the `coord doctor` fold excludes it.
    assert any(f.check == "clones.missing" for f in report.findings)


# ── The CLI ────────────────────────────────────────────────────────────────


def test_doctor_on_an_unknown_machine_still_renders_a_named_finding(config_path):
    result = CliRunner().invoke(machine_doctor, ["ghost", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "config.machine_missing" in result.output
    assert "coord machine add ghost" in result.output


def test_doctor_exits_nonzero_on_a_crit(config_path, cfg):
    health = _health(repos=[], degraded={"api": "repo_path missing"})
    with patch("coord.network.check_all",
               return_value=[_status(cfg.machines[0], health=health)]), \
         patch("coord.network.tailscale_ip_map", return_value={}):
        result = CliRunner().invoke(
            machine_doctor, ["laptop", "--config", str(config_path)]
        )
    assert result.exit_code == 1
    assert "clones.missing" in result.output
    # And it must say out loud that linger was NOT checked, rather than
    # letting a clean-looking report imply it was.
    assert "linger was NOT checked" in result.output


# ── `coord machine add` ────────────────────────────────────────────────────


def _add(config_path, *args):
    return CliRunner().invoke(
        machine_add, [*args, "--config", str(config_path), "--no-verify-host"]
    )


def test_add_writes_a_loadable_entry_with_repo_names_as_repo_paths_keys(config_path):
    result = _add(
        config_path, "dell64", "--host", "dell64.tail1234.ts.net",
        "--capabilities", "python,rust", "--repos", "api,web",
    )
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)  # must still load — that is the seatbelt
    machine = next(m for m in cfg.machines if m.name == "dell64")
    assert machine.host == "dell64.tail1234.ts.net"
    assert machine.capabilities == ["python", "rust"]
    assert machine.repos == ["api", "web"]
    assert machine.repo_paths == {"api": "~/src/api", "web": "~/src/web"}
    # The pre-existing machine and every comment must survive the edit.
    assert any(m.name == "laptop" for m in cfg.machines)


def test_add_refuses_a_repo_name_that_is_not_in_the_repos_block(config_path):
    """Incident 4, prevented instead of diagnosed. `claude-coordinator` is the
    checkout DIRECTORY; the repo NAME is what `repos:` says."""
    before = config_path.read_text()
    result = _add(
        config_path, "dell64", "--host", "dell64.tail1234.ts.net",
        "--repos", "api,acme-api",
    )
    assert result.exit_code != 0
    assert "acme-api" in result.output
    assert "fail to load" in result.output
    assert config_path.read_text() == before  # nothing written


def test_add_refuses_a_machine_that_already_exists(config_path):
    before = config_path.read_text()
    result = _add(config_path, "laptop", "--host", "laptop.tail1234.ts.net")
    assert result.exit_code != 0
    assert "coord machine doctor laptop" in result.output
    assert config_path.read_text() == before


def test_add_refuses_a_host_that_resolves_off_tailnet(config_path):
    """Incident 2, prevented instead of diagnosed — and the refusal names the
    MagicDNS FQDN to use instead."""
    before = config_path.read_text()
    with patch("coord.network.tailscale_ip_map",
               return_value={"dell64": ("100.64.0.9", "dell64.tail1234.ts.net")}), \
         patch("coord.network.resolve_host_ip", return_value="192.168.1.44"):
        result = CliRunner().invoke(
            machine_add,
            ["dell64", "--host", "dell64", "--config", str(config_path)],
        )
    assert result.exit_code != 0
    assert "dell64.tail1234.ts.net" in result.output
    assert config_path.read_text() == before


def test_add_writes_when_the_host_verifies(config_path):
    with patch("coord.network.tailscale_ip_map",
               return_value={"dell64": ("100.64.0.9", "dell64.tail1234.ts.net")}), \
         patch("coord.network.resolve_host_ip", return_value="100.64.0.9"):
        result = CliRunner().invoke(
            machine_add,
            ["dell64", "--host", "dell64.tail1234.ts.net", "--config", str(config_path)],
        )
    assert result.exit_code == 0, result.output
    assert load_config(config_path).machines[-1].name == "dell64"


def test_add_dry_run_writes_nothing_but_shows_the_result(config_path):
    before = config_path.read_text()
    result = _add(
        config_path, "dell64", "--host", "dell64.tail1234.ts.net",
        "--repos", "api", "--dry-run",
    )
    assert result.exit_code == 0, result.output
    assert "--dry-run: would write" in result.output
    assert "- name: dell64" in result.output
    assert config_path.read_text() == before


def test_add_prints_the_residue_it_deliberately_did_not_do(config_path):
    """The honest-residue rule #2220 established: a command that pretends
    completeness is how a machine looks onboarded while six things are
    silently broken."""
    result = _add(
        config_path, "dell64", "--host", "dell64.tail1234.ts.net", "--repos", "api",
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "NOT DONE" in out
    assert "install-agent.sh" in out          # incident 1
    assert "config-free" in out               # incident 3
    assert "~/src/api" in out                 # the clone
    assert "enable-linger" in out             # linger
    assert "graphify" in out                  # incident 6
    assert "coord machine doctor dell64" in out


def test_add_honours_a_repo_path_override(config_path):
    result = _add(
        config_path, "dell64", "--host", "h", "--repos", "api",
        "--repo-path", "api=/opt/checkouts/acme-api",
    )
    assert result.exit_code == 0, result.output
    machine = load_config(config_path).machines[-1]
    # The KEY stays the repo name even when the VALUE is an unrelated dir —
    # that separation is the whole lesson of incident 4.
    assert machine.repo_paths == {"api": "/opt/checkouts/acme-api"}


def test_add_rejects_a_repo_path_override_for_a_repo_it_is_not_adding(config_path):
    before = config_path.read_text()
    result = _add(
        config_path, "dell64", "--host", "h", "--repos", "api",
        "--repo-path", "web=/opt/web",
    )
    assert result.exit_code != 0
    assert config_path.read_text() == before


# ── `coord doctor` — the fleet report grows the machine layer ──────────────


def _run_doctor(config_path, monkeypatch, statuses):
    import coord.network as network_mod  # noqa: PLC0415
    from coord.commands.status import doctor  # noqa: PLC0415

    monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: statuses)
    monkeypatch.setattr(network_mod, "tailscale_ip_map", lambda *a, **k: None)
    return CliRunner().invoke(
        doctor, ["--config", str(config_path), "--no-pypi"], catch_exceptions=False
    )


def test_coord_doctor_surfaces_a_half_onboarded_machine(
    config_path, cfg, monkeypatch
):
    """#2915's wiring ask: a half-onboarded MACHINE shows up in the fleet
    report without anyone remembering to run the dedicated command.

    The repo-clone defect itself is already reported by the pre-existing
    #1712 cross-check (`_health_vs_config_lines`, "CRIT repos: ..."), so the
    onboarding fold must not print it a second time under the
    `clones.missing` name — that duplication was caught in review of #2915."""
    health = _health(
        repos=[], degraded={"api": "repo_path ~/src/api does not exist"},
        health={"results": [
            {"check_id": "graphify_cli", "severity": "warn", "headroom": "not installed"},
            {"check_id": "agent_venv", "severity": "crit", "headroom": "partial venv"},
        ]},
    )
    result = _run_doctor(
        config_path, monkeypatch, [_status(cfg.machines[0], health=health)]
    )
    assert result.exit_code == 1, result.output
    # The machine-onboarding fold must not print its own `clones.*` name for
    # a defect the #1712 cross-check immediately above it already reported
    # (as `CRIT repos: ...` + the `degraded` detail line) — that was the
    # literal duplication caught in review of #2915.
    assert "clones.missing" not in result.output
    assert "graph.graphify_missing" in result.output
    assert "runtime.agent_venv_broken" in result.output


def test_coord_doctor_stays_quiet_for_a_healthy_machine(config_path, cfg, monkeypatch):
    """The other half of the ask: a report that is always red is a report
    nobody reads. WARN-level residue must not leak into the fleet report."""
    health = _health(health={"results": [
        {"check_id": "graphify_cli", "severity": "ok", "headroom": "graphify 1.2.3"},
        {"check_id": "agent_venv", "severity": "ok", "headroom": "non-editable"},
        # A graph that exists but is STALE is WARN-level residue: real, but a
        # state the fleet runs in deliberately while the agent self-heals.
        {"check_id": "graph", "severity": "warn",
         "headroom": "128.8h stale", "subject": "api",
         "values": {"present": True}},
    ]})
    result = _run_doctor(
        config_path, monkeypatch, [_status(cfg.machines[0], health=health)]
    )
    assert "onboarding:" not in result.output
