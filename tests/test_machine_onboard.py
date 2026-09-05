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


# ── #2937: coord must resolve on a WORKER-shaped PATH, not just the agent's ──


def test_probe_coord_on_worker_path_parses_a_found_binary():
    with patch("subprocess.run", return_value=_ssh_result(
        "COORD_ON_WORKER_PATH_OK=1\nVERSION=coord, version 0.9.0\n"
    )):
        found, error, version = machine_onboard.probe_coord_on_worker_path("host")
    assert found is True
    assert error is None
    assert version == "coord, version 0.9.0"


def test_probe_coord_on_worker_path_reports_absence_as_a_defect_not_unknown():
    """`found=False` is the whole point of #2937 — it must never collapse
    into the fail-soft UNKNOWN path the way an SSH outage does."""
    with patch("subprocess.run", return_value=_ssh_result("COORD_ON_WORKER_PATH_OK=0\n")):
        found, error, version = machine_onboard.probe_coord_on_worker_path("host")
    assert found is False
    assert error is None
    assert version is None


def test_probe_coord_on_worker_path_delegates_to_the_canonical_agent_function():
    """#2937 review: this must not be a second, hand-rolled reimplementation
    of the #402/#2569 PATH strip — it has to invoke the exact same
    `coord.agent.worker_coord_reachable()` a real worker spawn's environment
    is built from, on the worker host's own pinned interpreter, so the two
    checks can never silently disagree (e.g. on a trailing slash, or a PATH
    entry that's already a resolved `.blue`/`.green` path)."""
    with patch("subprocess.run", return_value=_ssh_result(
        "COORD_ON_WORKER_PATH_OK=1\nVERSION=coord, version 0.9.0\n"
    )) as run:
        machine_onboard.probe_coord_on_worker_path("host")
    args, kwargs = run.call_args
    argv = args[0]
    assert argv[0] == "ssh"
    assert argv[-1] == "$HOME/.coord-venv/bin/python3 -"
    script = kwargs["input"]
    assert "from coord.agent import worker_coord_reachable" in script
    assert "worker_coord_reachable()" in script


def test_probe_coord_on_worker_path_fails_soft_when_the_pinned_interpreter_is_gone():
    """If `~/.coord-venv/bin/python3` itself doesn't resolve on the remote
    shell (e.g. the venv was never installed), the remote shell reports a
    nonzero exit — same fail-soft UNKNOWN discipline as any other SSH-layer
    trouble, never a fabricated found/absent."""
    with patch("subprocess.run", return_value=_ssh_result(
        "", returncode=127, stderr="bash: line 1: .coord-venv/bin/python3: No such file or directory"
    )):
        found, error, version = machine_onboard.probe_coord_on_worker_path("host")
    assert found is None
    assert "No such file" in error
    assert version is None


def test_probe_coord_on_worker_path_fails_soft_on_ssh_trouble():
    """Same discipline as `probe_linger`: an SSH-layer failure is UNKNOWN,
    never fabricated as either a pass or the #2937 defect."""
    with patch("subprocess.run",
               return_value=_ssh_result("", returncode=255, stderr="No route to host")):
        found, error, version = machine_onboard.probe_coord_on_worker_path("host")
    assert found is None
    assert "No route to host" in error
    assert version is None

    with patch("subprocess.run", return_value=_ssh_result("garbage, no marker\n")):
        found, error, version = machine_onboard.probe_coord_on_worker_path("host")
    assert found is None
    assert "no parseable output" in error
    assert version is None


def test_coord_absent_from_worker_path_crits():
    """The #2937 regression itself: dell64 shape — the agent is perfectly
    healthy (runtime.agent_venv is OK, /health answers fine) but `coord`
    cannot be found once ~/.coord-venv/bin is stripped from PATH, so a
    worker dispatched here can never run `coord test` to record a verdict."""
    facts = MachineFacts(
        name="dell64", configured=True, host="dell64.tail1234.ts.net",
        declared_capabilities=["python"], declared_repos=["api"],
        repo_paths={"api": "~/src/api"}, known_repos=["api"],
        coord_on_worker_path=False,
    )
    finding = _by_check(machine_onboard.evaluate(facts), "runtime.coord_on_worker_path_missing")
    assert finding.severity == CRIT
    assert "worker" in finding.summary.lower()
    assert "~/.coord-venv" in finding.fix


def test_coord_on_worker_path_unknown_without_the_ssh_probe(cfg):
    """Mirrors `runtime.linger_unknown`: /health cannot see this either — the
    agent process answering a probe still has its own venv on PATH, which
    proves nothing about a worker's shell."""
    report = machine_onboard.evaluate(_facts(cfg))
    finding = _by_check(report, "runtime.coord_on_worker_path_unknown")
    assert finding.severity == UNKNOWN
    assert "--ssh" in finding.summary


def test_coord_on_worker_path_matching_the_agent_version_is_ok():
    facts = MachineFacts(
        name="laptop", configured=True, host="laptop.tail1234.ts.net",
        declared_capabilities=["python"], declared_repos=["api"],
        repo_paths={"api": "~/src/api"}, known_repos=["api"],
        version="0.9.0",
        coord_on_worker_path=True,
        coord_on_worker_path_version="coord, version 0.9.0",
    )
    finding = _by_check(machine_onboard.evaluate(facts), "runtime.coord_on_worker_path")
    assert finding.severity == OK


def test_coord_on_worker_path_version_skew_from_the_agent_warns():
    """A worker-PATH `coord` that answers but on a DIFFERENT version than the
    agent's own /health is real residue — a worker could be recording
    verdicts from a stale or unrelated install — but not the #2937 total-loss
    case, so it warns rather than CRITs."""
    facts = MachineFacts(
        name="laptop", configured=True, host="laptop.tail1234.ts.net",
        declared_capabilities=["python"], declared_repos=["api"],
        repo_paths={"api": "~/src/api"}, known_repos=["api"],
        version="0.9.0",
        coord_on_worker_path=True,
        coord_on_worker_path_version="coord, version 0.7.1",
    )
    finding = _by_check(
        machine_onboard.evaluate(facts), "runtime.coord_on_worker_path_version_mismatch"
    )
    assert finding.severity == WARN
    assert "0.9.0" in finding.summary
    assert "0.7.1" in finding.summary


def test_coord_on_worker_path_version_comparison_is_exact_not_substring():
    """#2937 review: `expected in reported` is a substring test, and this
    project's own version scheme makes it a trap — "0.5.29" IS a substring
    of "coord, version 0.5.290" even though those are different releases.
    A genuinely-skewed patch version must still warn."""
    facts = MachineFacts(
        name="laptop", configured=True, host="laptop.tail1234.ts.net",
        declared_capabilities=["python"], declared_repos=["api"],
        repo_paths={"api": "~/src/api"}, known_repos=["api"],
        version="0.5.29",
        coord_on_worker_path=True,
        coord_on_worker_path_version="coord, version 0.5.290",
    )
    finding = _by_check(
        machine_onboard.evaluate(facts), "runtime.coord_on_worker_path_version_mismatch"
    )
    assert finding.severity == WARN
    assert "0.5.29" in finding.summary
    assert "0.5.290" in finding.summary


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


# ══════════════════════════════════════════════════════════════════════════
# #3137 — layer 7 (toolchain), layer 8 (identity), and the role dimension
#
# Six layers passed `precision` with `crit=0 ... ok=true` while it had no
# restic, no ~/.coord/backup.env and no daemon units. These tests are the
# gate that failure could not reach: one named finding per gap, each one
# reachable from seeded facts with no network, no SSH and no live agent.
# ══════════════════════════════════════════════════════════════════════════


def _probe(tool, *, found=True, version=None, min_version=None, meets_floor=None,
           capability=None):
    from coord.prereqs import ToolProbe

    return ToolProbe(
        tool=tool, capability=capability, found=found, version=version,
        min_version=min_version, meets_floor=meets_floor, what_breaks="",
    )


def _tc_facts(**kw):
    """A reachable, otherwise-clean machine, with layer 7/8 facts seeded."""
    base = dict(
        name="laptop", configured=True, host="laptop.tail1234.ts.net",
        declared_capabilities=["rust"], declared_repos=["api"],
        repo_paths={"api": "~/src/api"}, known_repos=["api"],
        host_matches_tailnet=True, reachable=True, version="0.9.0",
        published_capabilities=["rust"], published_repos=["api"],
    )
    base.update(kw)
    return MachineFacts(**base)


def _find(findings, check):
    return next((f for f in findings if f.check == check), None)


# ── Acceptance 1: the #1671 regression ─────────────────────────────────────


def test_tool_on_login_path_but_not_the_agents_path_crits_naming_both_paths():
    """THE finding this layer exists for. On 2026-08-01 `cargo` was installed,
    invisible to the agent, the `rust` probe read "not found", dispatch_smoke
    refused to route, and the Test stage retried every 30s with no
    board-visible reason. `/health` structurally cannot report this: its
    probes run inside the agent's own process, so "absent" and "present but
    off my PATH" are the same answer there. Only the login-shell comparison
    tells them apart — and the CRIT must name BOTH paths, because the fix
    (edit the agent's unit) is not the fix a reader would otherwise guess
    (edit their own shell rc)."""
    facts = _tc_facts(
        tool_probes={"cargo": _probe("cargo", found=False, capability="rust")},
        shell_probed=True,
        login_path_tools={"cargo": "/home/john/.cargo/bin/cargo"},
        login_path="/home/john/.cargo/bin:/usr/bin",
        agent_path="/home/john/.coord-venv/bin:/usr/bin",
    )
    finding = _find(machine_onboard.evaluate_toolchain(facts), "toolchain.tool_off_agent_path")
    assert finding is not None
    assert finding.severity == CRIT
    # Both paths, named.
    assert "/home/john/.cargo/bin/cargo" in finding.summary
    assert "/home/john/.coord-venv/bin:/usr/bin" in finding.summary
    # And the fix must point at the AGENT's unit, not the operator's shell.
    assert "coord-agent" in (finding.fix or "")


def test_a_genuinely_absent_tool_is_not_reported_as_the_path_trap():
    """The counterweight: if the login shell cannot find it either, this is an
    ordinary missing tool and must not be dressed up as a PATH problem —
    otherwise the #1671 finding stops meaning anything."""
    facts = _tc_facts(
        tool_probes={"cargo": _probe("cargo", found=False, capability="rust")},
        shell_probed=True, login_path_tools={"cargo": None},
    )
    checks = {f.check for f in machine_onboard.evaluate_toolchain(facts)}
    assert "toolchain.tool_missing" in checks
    assert "toolchain.tool_off_agent_path" not in checks


def test_missing_tool_without_ssh_says_it_could_not_tell_the_trap_apart():
    """Absence of evidence, stated out loud: with no login-shell probe the
    layer still CRITs (the tool really is unusable) but must say it cannot
    distinguish the #1671 trap, rather than implying it ruled it out."""
    facts = _tc_facts(
        tool_probes={"cargo": _probe("cargo", found=False, capability="rust")},
    )
    finding = _find(machine_onboard.evaluate_toolchain(facts), "toolchain.tool_missing")
    assert finding.severity == CRIT
    assert "--ssh" in finding.summary


def test_probe_binaries_excludes_the_pkg_config_backed_gtk4_prereq():
    """`gtk4`'s binary is `pkg-config` and its probe is a MODULE lookup, so a
    login shell finding pkg-config says nothing about GTK4 dev libs — feeding
    it into the comparison would manufacture a #1671 CRIT out of a machine
    that simply has no GTK. The exclusion is structural (`tool == binary`),
    not a hardcoded skip list."""
    binaries = machine_onboard.probe_binaries(["gtk", "rust"], "worker")
    assert "cargo" in binaries
    assert "pkg-config" not in binaries


# ── Acceptance 2: a tool below a known floor ───────────────────────────────


def test_tool_below_its_version_floor_crits_naming_floor_and_found_version():
    """`gh` is the floor that already exists (GH_PR_CHECKS_JSON_MIN_VERSION):
    a daemon on a too-old gh cannot read CI status, so every production merge
    gate silently degrades. The CRIT must carry both numbers — "upgrade gh" is
    not actionable without knowing to what, or from what."""
    from coord.github_ops import GH_PR_CHECKS_JSON_MIN_VERSION

    facts = _tc_facts(
        declared_capabilities=[],
        tool_probes={
            "gh": _probe("gh", version="2.0.0",
                         min_version=GH_PR_CHECKS_JSON_MIN_VERSION, meets_floor=False),
        },
    )
    finding = _find(machine_onboard.evaluate_toolchain(facts), "toolchain.tool_below_floor")
    assert finding.severity == CRIT
    assert "2.0.0" in finding.summary
    assert GH_PR_CHECKS_JSON_MIN_VERSION in finding.summary
    assert GH_PR_CHECKS_JSON_MIN_VERSION in (finding.fix or "")


def test_the_floor_is_coord_prereqs_own_not_a_second_copy():
    """One question, one answer (#2085): this layer must not re-derive whether
    a version passes. It reads `ToolProbe.ok`, the same predicate
    `prereqs.unmet_capabilities` applies for layer 3 — so a probe the shared
    predicate calls fine can never CRIT here."""
    from coord.prereqs import unmet_capabilities

    probe = _probe("cargo", version="1.0.0", min_version="0.9",
                   meets_floor=True, capability="rust")
    assert probe.ok
    assert unmet_capabilities(["rust"], {"cargo": probe}) == {}
    facts = _tc_facts(tool_probes={"cargo": probe})
    assert not [f for f in machine_onboard.evaluate_toolchain(facts) if f.severity == CRIT]


def test_layer_7_and_layer_3_cannot_disagree_about_an_unmet_capability():
    """The split-brain guard. Layer 3 reports the CAPABILITY roll-up and layer
    7 the TOOL detail, from the same /health probes through the same
    predicate. If layer 7 CRITs on a tool, layer 3 must be reporting its
    capability unmet — two surfaces answering the same question must agree by
    construction, not by coincidence."""
    from coord.prereqs import unmet_capabilities

    probes = {"cargo": _probe("cargo", found=False, capability="rust")}
    facts = _tc_facts(
        tool_probes=probes,
        unmet_capabilities=unmet_capabilities(["rust"], probes),
    )
    report = machine_onboard.evaluate(facts)
    tool_crits = [f for f in report.for_layer("toolchain") if f.severity == CRIT]
    cap_crits = [f for f in report.for_layer("agent") if f.check == "agent.capability_unmet"]
    assert tool_crits and cap_crits
    assert {f.subject for f in cap_crits} == {"rust"}


def test_a_capability_no_prereq_backs_warns_rather_than_passing_silently():
    facts = _tc_facts(declared_capabilities=["teleportation"], tool_probes={"git": _probe("git")})
    finding = _find(machine_onboard.evaluate_toolchain(facts), "toolchain.capability_unmapped")
    assert finding.severity == WARN
    assert finding.subject == "teleportation"


def test_toolchain_is_unknown_not_ok_when_no_agent_answered():
    """A gate that reads a dead agent as clean is not a gate."""
    facts = _tc_facts(reachable=False)
    findings = machine_onboard.evaluate_toolchain(facts)
    assert [f.severity for f in findings] == [UNKNOWN]


def test_role_tool_missing_still_fires_when_the_agent_is_unreachable():
    """The review-blocking regression: a daemon host straight off a fresh OS
    install has SSH up but coord-agent not yet running (#3137's own
    motivating scenario — "dellserver ... rebuilt from a fresh OS"). The
    capability/version verdicts genuinely need /health and cannot be
    produced without it, but the role-tool check (restic on a daemon) is
    gathered entirely over SSH via `facts.shell_probed` /
    `facts.login_path_tools` and has nothing to do with agent reachability.
    Dropping it here would silently swallow the exact restic CRIT the whole
    layer was written to surface."""
    facts = _tc_facts(
        reachable=False, role="daemon", role_source="file",
        shell_probed=True, login_path_tools={"restic": None},
    )
    findings = machine_onboard.evaluate_toolchain(facts)
    assert _find(findings, "toolchain.unprobed").severity == UNKNOWN
    restic = _find(findings, "toolchain.role_tool_missing")
    assert restic is not None
    assert restic.severity == CRIT
    assert restic.subject == "restic"


# ── Acceptance 3: the forge token, and the role that decides its bar ───────


def _identity(**kw):
    return machine_onboard.IdentityFacts(probed=True, **kw)


def test_a_machine_with_no_forge_token_crits():
    facts = _tc_facts(
        shell_probed=True,
        identity=_identity(forge_token_present=False, forge_repo_read=False,
                           forge_repo="acme/api", forge_reason="gh: not authenticated"),
    )
    finding = _find(machine_onboard.evaluate_identity(facts), "identity.forge_read_missing")
    assert finding.severity == CRIT
    assert "acme/api" in finding.summary
    assert "gh auth login" in (finding.fix or "")


def test_a_token_that_cannot_merge_crits_on_a_daemon():
    """Presence is not capability — the #3129 distinction. `coord merge`
    re-invokes itself on the daemon, so it is the DAEMON's token that decides
    every production merge; one that reads fine and cannot push is a merge
    queue that wedges with no explanation."""
    facts = _tc_facts(
        role="daemon", role_source="file", shell_probed=True,
        identity=_identity(forge_token_present=True, forge_repo_read=True,
                           forge_can_merge=False, forge_repo="acme/api"),
    )
    finding = _find(machine_onboard.evaluate_identity(facts), "identity.forge_merge_missing")
    assert finding.severity == CRIT
    assert "acme/api" in finding.summary


def test_a_token_that_cannot_merge_is_silent_on_a_thin_client():
    """The role dimension's whole point. #3128 spells exactly two roles, and
    its `worker` default IS #3137's "thin client" column — a machine that
    never merges is not a degraded daemon, and warning it about merge rights
    is how a report stops being read. Silence here must be TOTAL: not a WARN,
    not an UNKNOWN, no finding at all."""
    facts = _tc_facts(
        role="worker", role_source="file", shell_probed=True,
        identity=_identity(forge_token_present=True, forge_repo_read=True,
                           forge_can_merge=False, forge_repo="acme/api"),
    )
    checks = {f.check for f in machine_onboard.evaluate_identity(facts)}
    assert not any(c.startswith("identity.forge_merge") for c in checks)
    # ... while the read check, which DOES apply to every role, still fires.
    assert "identity.forge_read" in checks


def test_daemon_role_demands_the_dr_lane_and_a_worker_is_not_asked_for_it():
    """Run against precision (a worker) today this must stay quiet about
    restic and backup.env, and report both the moment it is declared a
    daemon — that difference is the role-awareness proof."""
    kw = dict(
        shell_probed=True, login_path_tools={"restic": None},
        identity=_identity(backup_env_present=False),
    )
    worker = machine_onboard.evaluate(_tc_facts(role="worker", role_source="file", **kw))
    daemon = machine_onboard.evaluate(_tc_facts(role="daemon", role_source="file", **kw))

    worker_checks = {f.check for f in worker.findings}
    assert "toolchain.role_tool_missing" not in worker_checks
    assert not any(c.startswith("identity.backup_env") for c in worker_checks)

    restic = _find(daemon.findings, "toolchain.role_tool_missing")
    assert restic.severity == CRIT and restic.subject == "restic"
    assert _find(daemon.findings, "identity.backup_env_missing").severity == CRIT
    assert not daemon.ok


def test_board_token_verdict_is_the_daemons_answer_not_the_files_existence():
    """#2096: a reported outcome that only proves a file parses is not
    evidence. The verdict is whether the DAEMON accepted the token."""
    facts = _tc_facts(
        shell_probed=True,
        identity=_identity(board_token_present=True, board_token_accepted=False,
                           board_reason="HTTPStatusError: 401 unauthorized"),
    )
    finding = _find(machine_onboard.evaluate_identity(facts), "identity.board_token_missing")
    assert finding.severity == CRIT
    assert "401" in finding.summary


def test_identity_without_the_ssh_probe_is_unknown_never_a_pass():
    facts = _tc_facts()
    findings = machine_onboard.evaluate_identity(facts)
    for check in ("forge_read", "git_push", "claude_oauth", "board_token"):
        assert _find(findings, f"identity.{check}_unknown").severity == UNKNOWN
    assert not [f for f in findings if f.severity in (CRIT, WARN)]


def test_identity_tailnet_defers_to_layer_2_rather_than_double_counting():
    """The same #2912 fact, read once. Layer 2 owns the CRIT of record; layer
    8 naming it again at CRIT would count one defect twice in one exit code."""
    facts = _tc_facts(host_matches_tailnet=False)
    report = machine_onboard.evaluate(facts)
    assert _find(report.findings, "network.host_resolves_offtailnet").severity == CRIT
    mirrored = _find(report.findings, "identity.tailnet_node_mismatch")
    assert mirrored.severity == WARN
    assert "network.host_resolves_offtailnet" in mirrored.summary


# ── Acceptance 4: no credential value, anywhere ────────────────────────────


SECRETS = (
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    "github_pat_11ABCDEFG0abcdefghijklmnop",
    "sk-ant-oat01-SUPERSECRETVALUE-abcdef",
    "b0ardT0kenb0ardT0kenb0ardT0kenb0ardT0ken",
)


def test_no_credential_value_reaches_any_finding_log_line_or_summary():
    """This layer touches every secret the fleet has. The probe is written
    never to emit one — but a free-form error string from `gh`, `ssh` or an
    HTTP client is attacker-adjacent text pasted straight into a report, so
    the parse boundary redacts as an independent second line of defence.
    Asserted on CAPTURED OUTPUT, over a payload that maliciously carries a
    token in every reason field."""
    import json as _json

    payload = {
        "login_path_tools": {"cargo": "/usr/bin/cargo"},
        "login_path": "/usr/bin", "agent_path": "/usr/bin",
        "role": {"role": "daemon", "source": "file", "valid": True, "raw": "daemon"},
        "identity": {
            "forge_token_present": True, "forge_repo_read": False,
            "forge_repo": "acme/api",
            "forge_reason": f"gh: bad credentials for {SECRETS[0]}",
            "git_push_ok": False,
            "git_push_reason": f"key rejected: {SECRETS[1]}",
            "claude_oauth_present": False,
            "claude_oauth_reason": f"stale creds {SECRETS[2]}",
            "board_token_present": True, "board_token_accepted": False,
            "board_reason": f"401 for Bearer {SECRETS[3]}",
            "backup_env_present": False,
        },
    }
    probe = machine_onboard.parse_shell_probe(
        "COORD_MACHINE_PROBE=" + _json.dumps(payload)
    )
    facts = _tc_facts(
        role=probe.role, role_source=probe.role_source, shell_probed=True,
        login_path_tools=probe.login_path_tools, login_path=probe.login_path,
        agent_path=probe.agent_path, identity=probe.identity,
    )
    rendered = "\n".join(
        machine_onboard.format_report(machine_onboard.evaluate(facts), verbose=True)
    )
    # The report is genuinely populated — otherwise this passes vacuously.
    assert "identity.forge_read_missing" in rendered
    assert "identity.board_token_missing" in rendered
    for secret in SECRETS:
        assert secret not in rendered, secret
        assert secret not in repr(facts.identity), secret


def test_redaction_leaves_diagnostics_readable():
    """A redactor that eats the PATH it is supposed to print is useless: the
    finding still has to say WHERE the tool was found."""
    assert machine_onboard.redact("gh: HTTP 401 Bad credentials") == (
        "gh: HTTP 401 Bad credentials"
    )
    facts = _tc_facts(
        tool_probes={"cargo": _probe("cargo", found=False, capability="rust")},
        shell_probed=True, login_path_tools={"cargo": "/home/john/.cargo/bin/cargo"},
        agent_path="/home/john/.coord-venv/bin:/usr/bin",
    )
    finding = _find(machine_onboard.evaluate_toolchain(facts), "toolchain.tool_off_agent_path")
    assert "/home/john/.cargo/bin/cargo" in finding.summary


# ── Acceptance 5: the role comes from #3128, and defaults exactly as it does ─


def test_absent_role_declaration_behaves_as_worker_and_adds_no_new_findings():
    """#3128's first acceptance criterion, inherited: a host that never opts
    in must be graded byte-for-byte as it is today. `worker` explicitly
    declared and `worker` by default must produce identical findings — and
    neither may produce a single new CRIT or WARN on an otherwise-clean
    machine."""
    default_role = machine_onboard.evaluate(_tc_facts(
        tool_probes={"cargo": _probe("cargo", version="1.80.0", capability="rust")},
    ))
    declared = machine_onboard.evaluate(_tc_facts(
        role="worker", role_source="file",
        tool_probes={"cargo": _probe("cargo", version="1.80.0", capability="rust")},
    ))
    # The only permitted difference is the line that NAMES the role and where
    # it came from — the grading itself must be identical.
    role_lines = {"identity.role", "identity.role_undeclared"}

    def _graded(report):
        return [(f.check, f.severity) for f in report.findings if f.check not in role_lines]

    assert _graded(default_role) == _graded(declared)
    assert default_role.crits == []
    assert default_role.warns == []
    assert default_role.ok


def test_the_role_is_read_through_3128s_resolver_only():
    """No second reader and no second default: the SSH probe CALLS
    `deploy_manifest.resolve_role` on the host that owns the declaration, and
    this module only ever consumes the RoleDeclaration it returns."""
    from coord import deploy_manifest

    script = machine_onboard._SHELL_PROBE_SCRIPT
    assert "resolve_role" in script
    # Neither source #3128 owns may be read here directly — not the file...
    assert "~/.coord/role" not in script
    # ... and not the env var, whose NAME must appear nowhere but #3128.
    assert deploy_manifest.ROLE_ENV_VAR not in script
    # And the vocabulary is #3128's, not a copy.
    assert set(machine_onboard.ROLE_IDENTITY_CHECKS) <= set(deploy_manifest.ROLE_UNITS)
    assert MachineFacts(name="x").role == deploy_manifest.ROLE_WORKER


def test_a_role_declaration_that_is_not_a_role_warns_rather_than_being_swallowed():
    facts = _tc_facts(role="worker", role_source="file", role_raw="deamon", role_valid=False)
    finding = _find(machine_onboard.evaluate_identity(facts), "identity.role_invalid")
    assert finding.severity == WARN
    assert "deamon" in finding.summary


# ── Acceptance 6: the exit code still gates ────────────────────────────────


def test_doctor_exits_nonzero_on_a_toolchain_crit(config_path, cfg):
    """M2 is going to be built against this exit code, so a layer-7 CRIT has
    to reach it — a finding that renders but cannot fail the gate is
    decoration."""
    health = _health(tool_versions={
        "python3": {"capability": "python", "found": False, "version": None},
    })
    with patch("coord.network.check_all",
               return_value=[_status(cfg.machines[0], health=health)]), \
         patch("coord.network.tailscale_ip_map", return_value={}):
        result = CliRunner().invoke(
            machine_doctor, ["laptop", "--config", str(config_path)]
        )
    assert result.exit_code == 1
    assert "toolchain.tool_missing" in result.output
    assert "layers 7-8 are PARTIAL without --ssh" in result.output


def test_doctor_role_flag_grades_a_worker_against_the_daemon_bar(config_path, cfg):
    """`--role daemon` answers "what would precision be missing if it were the
    daemon host?" without touching the host's own declaration."""
    with patch("coord.network.check_all",
               return_value=[_status(cfg.machines[0], health=_health())]), \
         patch("coord.network.tailscale_ip_map", return_value={}):
        plain = CliRunner().invoke(
            machine_doctor, ["laptop", "--config", str(config_path), "-v"]
        )
        as_daemon = CliRunner().invoke(
            machine_doctor,
            ["laptop", "--config", str(config_path), "-v", "--role", "daemon"],
        )
    assert "identity.forge_merge" not in plain.output
    assert "identity.forge_merge" in as_daemon.output
    assert "identity.backup_env" in as_daemon.output
    assert "role 'daemon' (source: flag)" in as_daemon.output


# ── The probe itself: control flow, output shape, and no credential in it ──


def test_the_shell_probe_script_runs_end_to_end_without_touching_a_credential(
    tmp_path, monkeypatch, capsys
):
    """Executes the REAL embedded script against a stubbed `subprocess.run`
    and a stubbed HOME, so the thing that ships is what is exercised — a
    syntax check on a string proves nothing about its control flow. Asserts
    the emitted payload is booleans and paths only."""
    import json as _json
    import subprocess as _subprocess

    monkeypatch.setenv("HOME", str(tmp_path))
    coord_dir = tmp_path / ".coord"
    coord_dir.mkdir()
    (coord_dir / "role").write_text("daemon\n")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text('{"token": "sk-ant-oat01-x"}')
    (tmp_path / ".claude.json").write_text("{}")

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        joined = " ".join(argv) if isinstance(argv, list) else str(argv)
        if "command -v -- restic" in joined:
            return _subprocess.CompletedProcess(argv, 1, "", "")
        if "command -v" in joined:
            return _subprocess.CompletedProcess(argv, 0, "/usr/bin/cargo\n", "")
        if "$PATH" in joined:
            return _subprocess.CompletedProcess(argv, 0, "/usr/bin:/bin", "")
        if "MainPID" in joined:
            return _subprocess.CompletedProcess(argv, 0, "0\n", "")
        if "gh api" in joined:
            return _subprocess.CompletedProcess(argv, 0, "true\n", "")
        if "git@github.com" in joined:
            return _subprocess.CompletedProcess(
                argv, 1, "", "Hi john! You've successfully authenticated"
            )
        return _subprocess.CompletedProcess(argv, 1, "", "unexpected")

    from coord import client as client_mod

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(client_mod, "resolve_board_service",
                        lambda *a, **k: client_mod.ServiceConfig(url="http://d:7435", token="t"))
    monkeypatch.setattr(client_mod, "fetch_board_payload", lambda *a, **k: {"ok": True})

    params = _json.dumps({"binaries": ["cargo", "restic"], "repo_slug": "acme/api"})
    script = machine_onboard._SHELL_PROBE_SCRIPT.replace("__PARAMS__", repr(params))
    exec(compile(script, "<probe>", "exec"), {"__name__": "__probe__"})  # noqa: S102

    out = capsys.readouterr().out
    probe = machine_onboard.parse_shell_probe(out)

    # It really did ask #3128's resolver, on the host, and got the file's answer.
    assert probe.role == "daemon"
    assert probe.role_source == "file"
    # Capability, not just presence.
    assert probe.identity.forge_repo_read is True
    assert probe.identity.forge_can_merge is True
    assert probe.identity.git_push_ok is True
    assert probe.identity.claude_oauth_present is True
    assert probe.identity.board_token_accepted is True
    assert probe.identity.backup_env_present is False
    assert probe.login_path_tools == {"cargo": "/usr/bin/cargo", "restic": None}

    # `gh auth status --show-token` must never appear, and no credential file
    # content may reach the payload — the Claude creds above contain a token.
    joined_calls = " ".join(
        " ".join(c) if isinstance(c, list) else str(c) for c in calls
    )
    assert "--show-token" not in joined_calls
    assert "sk-ant-oat01-x" not in out


def test_probe_machine_shell_redacts_the_exception_branch_too(monkeypatch):
    """The module's own stated invariant is that every free-form reason that
    survives into a Finding goes through `redact` as a second, independent
    line of defence — but the `except (OSError, subprocess.SubprocessError)`
    branch in `probe_machine_shell` built its `ShellProbe(error=...)` straight
    from `str(exc)`, unlike the non-zero-exit branch two lines below it. Not
    exploitable today (these exceptions' string forms don't carry command
    output), but it's a real gap in defense-in-depth the moment the except
    clause is broadened or what's captured changes — so assert the redactor
    runs here too, over an exception message that is deliberately
    secret-shaped."""
    import subprocess as _subprocess

    def fake_run(*a, **k):
        raise OSError(f"connect failed, cached key {SECRETS[0]}")

    monkeypatch.setattr(_subprocess, "run", fake_run)

    probe = machine_onboard.probe_machine_shell(
        "dead-host", binaries=["cargo"], repo_slug=None, timeout=1.0,
    )
    assert probe.error is not None
    assert SECRETS[0] not in probe.error
    assert machine_onboard.REDACTED in probe.error


def test_an_unparseable_probe_is_an_error_not_a_clean_bill():
    probe = machine_onboard.parse_shell_probe("login banner only\n")
    assert probe.error
    assert probe.identity.probed is False
    facts = _tc_facts(shell_probed=False, shell_probe_error=probe.error,
                      identity=machine_onboard.IdentityFacts(probed=False, error=probe.error))
    findings = machine_onboard.evaluate_identity(facts)
    assert _find(findings, "identity.board_token_unknown").severity == UNKNOWN


def test_the_cheap_second_pass_reads_as_unprobed_not_as_missing_credentials():
    """`gather_facts` re-probes ONLY when the host's own role turns out to
    want a binary the first pass never looked up, and that pass skips every
    credential check to avoid a second `gh api` + `ssh -T`. A skipped check
    must read UNKNOWN — a cheap pass that rendered as "this machine holds no
    credentials" would be the worst possible false CRIT in this layer."""
    probe = machine_onboard.parse_shell_probe(
        'COORD_MACHINE_PROBE={"login_path_tools": {"restic": "/usr/bin/restic"}, '
        '"identity": null}'
    )
    assert probe.identity.probed is False
    assert probe.identity.board_token_accepted is None
    assert probe.login_path_tools == {"restic": "/usr/bin/restic"}


def test_a_host_whose_agent_predates_3128_still_reports_its_identity():
    """Found by running this against precision on 2026-09-05, before it was
    a test: the fleet's pinned agent venv predated #3128, `resolve_role` did
    not import, and the WHOLE probe was discarded — login PATH and all four
    identity verdicts, over one unavailable import. A probe that reports
    nothing because one of its questions could not be asked is the same
    failure #3137 exists to end, so the role error is a SEPARATE field from
    "the probe produced no payload"."""
    import json as _json

    payload = {
        "login_path_tools": {"cargo": "/usr/bin/cargo"},
        "login_path": "/usr/bin",
        "agent_path": "/usr/bin",
        "role": {"error": "ImportError: cannot import name 'resolve_role'"},
        "identity": {"forge_repo_read": True, "board_token_accepted": True},
    }
    probe = machine_onboard.parse_shell_probe(
        "COORD_MACHINE_PROBE=" + _json.dumps(payload)
    )
    assert probe.error is None          # the payload arrived...
    assert probe.role_error             # ... only the role question failed
    assert probe.identity.probed is True
    assert probe.identity.board_token_accepted is True

    facts = _tc_facts(
        shell_probed=True, role_error=probe.role_error, role_source="unprobed",
        login_path_tools=probe.login_path_tools, identity=probe.identity,
    )
    findings = machine_onboard.evaluate_identity(facts)
    # The identity verdicts survive...
    assert _find(findings, "identity.forge_read").severity == OK
    assert _find(findings, "identity.board_token").severity == OK
    # ... and the role is honestly reported as unreadable, naming why.
    role = _find(findings, "identity.role_undeclared")
    assert role.severity == UNKNOWN
    assert "ImportError" in role.summary
    assert "--role" in role.summary
