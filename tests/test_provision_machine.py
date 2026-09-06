"""Black-box tests for ``scripts/provision-machine.sh`` (#3138).

These drive the **real script**, end to end, all twelve phases, against a
directory of stub binaries on ``$PATH`` and a throwaway ``$HOME``. Nothing is
mocked at a Python seam: the script shells out to ``coord``, ``systemctl``,
``git``, ``gh``, ``tailscale``, ``ssh`` and ``install-agent.sh`` exactly as it
does on a bare-metal box, and the assertions are on its *output* and on the
state it leaves behind on disk.

That is deliberate. #3138's acceptance criteria are all statements about
observable behaviour — "the second run changes nothing", "the prompts come
before the slow phases", "no credential reaches a log or a non-0600 file",
"it exits non-zero and names the failing layer" — and none of them survive
being restated as a unit test against an extracted helper.

What a stub fleet structurally cannot answer is "do these package names
resolve on the OS this targets?" and "does the thing the units start actually
answer?" — a stubbed ``apt-get`` says yes to everything and a stubbed ``curl``
invents a ``/health``. ``scripts/verify-provision-noble.sh`` answers those
against a real Ubuntu 24.04 root filesystem with no stubs at all: real
packages, a real PyPI venv, a real ``coord agent`` answering ``/health`` and a
real ``coord serve`` answering ``/board`` on the real default ports inside a
private network namespace, and every daemon unit rendered and parsed by
noble's own ``systemd-analyze``. (Its first run found the #1678 browser false
green that ``test_a_chromium_browser_that_is_only_a_name_on_path_is_not_a_browser``
below now pins.) What neither can reach is systemd as PID 1 — ``systemctl
--user enable --now``, linger, ``snap install`` — nor a real ``tailscale
up``/``gh auth login``, nor a live agent taking a dispatch: that is still the
throwaway-VM run the issue asks for, and it is in the PR's SMOKE_TESTS block,
not here.

The daemon-unit phase is the exception that proves the rule: it runs the
**real** ``coord.deploy_manifest`` / ``coord.deploy_units`` /
``unit_drift.packaged_unit_dir``, through the stub venv's Python, against the
repo's own ``coord/deploy/``. So `test_server_installs_exactly_the_manifests_units`
fails if ``ROLE_UNITS[daemon]`` and the packaged unit set ever disagree.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from coord.deploy_manifest import ROLE_DAEMON, units_for_role

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="provision-machine.sh is Ubuntu-only by design (systemd, GNU stat, apt)",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "provision-machine.sh"

# A settings checkout whose `machines:` block is last, so the `coord machine
# add` stub can append to it the way the real command's inserter does.
SETTINGS_CONFIG = """\
repos:
  - name: api
    github: acme/api
  - name: shared
    github: acme/shared

machines:
  - name: coordinator
    host: coordinator.tailnet
    capabilities: [python]
    repos: [api]
"""


# ── The stub fleet ───────────────────────────────────────────────────────────
#
# Every stub appends its own argv to $COORD_STUB_LOG. That log is what the
# "never creates a second user" / "never requires Azure" / "no credential in
# argv" tests assert on: not the script's *source*, which can lie, but the
# commands it actually ran.

_LOGGER = 'printf "%s\\n" "$(basename "$0") $*" >> "$COORD_STUB_LOG"\n'

_TRIVIAL = "exit 0\n"

STUBS: dict[str, str] = {
    # apt is only ever reached when a base probe genuinely failed.
    "apt-get": _TRIVIAL,
    # sudo must stay transparent: `sudo apt-get ...` has to reach the apt-get
    # stub, so the "did it try to install anything" assertions still see it.
    "sudo": 'exec "$@"\n',
    "id": """
case "$1" in
    -u)  printf '%s\\n' "${COORD_STUB_UID:-1000}" ;;
    -un) printf '%s\\n' "${COORD_STUB_USER:-tester}" ;;
    *)   printf 'uid=1000\\n' ;;
esac
exit 0
""",
    "useradd": 'printf "SECOND-USER\\n" >> "$COORD_STUB_LOG"; exit 1\n',
    "adduser": 'printf "SECOND-USER\\n" >> "$COORD_STUB_LOG"; exit 1\n',
    "jq": "exit 1\n",  # forces the --host fallback; tests pass --host explicitly
    "tmux": _TRIVIAL,
    "rsync": _TRIVIAL,
    "unzip": _TRIVIAL,
    "gcc": _TRIVIAL,
    "rg": _TRIVIAL,
    "node": 'printf "v22.0.0\\n"; exit 0\n',
    "npm": _TRIVIAL,
    "claude": 'printf "1.0.0 (Claude Code)\\n"; exit 0\n',
    "restic": 'printf "restic 0.17.0\\n"; exit 0\n',
    "loginctl": _TRIVIAL,
    "mountpoint": _TRIVIAL,
    "chromium": _TRIVIAL,
    "cargo": 'printf "cargo 1.80.0\\n"; exit 0\n',
    "rustc": 'printf "rustc 1.80.0\\n"; exit 0\n',
    "pkg-config": """
case "$*" in
    *--exists*gtk4*)     exit 0 ;;
    *--modversion*gtk4*) printf '4.14.0\\n'; exit 0 ;;
esac
exit 0
""",
    "curl": """
for arg in "$@"; do
    case "$arg" in
        *"/health") printf '{"machine":"testbox","ok":true}\\n'; exit 0 ;;
    esac
done
exit 0
""",
    "ssh": 'exit "${COORD_STUB_SSH_RC:-0}"\n',
    "ssh-keygen": """
dest=""
while [ $# -gt 0 ]; do
    if [ "$1" = "-f" ]; then dest="$2"; shift 2; else shift; fi
done
[ -n "$dest" ] || exit 1
mkdir -p "$(dirname "$dest")"
printf 'PRIVATE-KEY-MATERIAL\\n' > "$dest"
chmod 0600 "$dest"
printf 'ssh-ed25519 AAAATESTKEYBODY testbox-coord\\n' > "$dest.pub"
exit 0
""",
    "tailscale": """
case "$*" in
    *--json*) printf '{"Self":{"DNSName":"testbox.tail.ts.net."}}\\n'; exit 0 ;;
    up*)      exit 0 ;;
    status*)  printf 'tailnet ok\\n'; exit 0 ;;
esac
exit 0
""",
    "gh": """
case "$1 $2" in
    "auth status")  exit 0 ;;
    "auth login")   exit 0 ;;
    "ssh-key list") cat "$HOME/.ssh/id_ed25519.pub" 2>/dev/null; exit 0 ;;
    "ssh-key add")  exit 0 ;;
esac
printf 'gh version 2.90.0 (stub)\\n'
exit 0
""",
    "git": """
if [ "$1" = "-C" ]; then
    dir="$2"; shift 2
    case "$1" in
        rev-parse)
            if [ -d "$dir/.git" ] && [ -f "$dir/.git/HEAD" ]; then
                printf 'deadbeef\\n'; exit 0
            fi
            exit 128 ;;
    esac
    exit 0
fi
if [ "$1" = "clone" ]; then
    dest=""
    for a in "$@"; do dest="$a"; done
    mkdir -p "$dest/.git"
    printf 'ref: refs/heads/main\\n' > "$dest/.git/HEAD"
    case "$dest" in
        *coord-settings)
            mkdir -p "$dest/coord"
            printf 'repos: []\\nmachines: []\\n' > "$dest/coord/coordinator.yml" ;;
    esac
    exit 0
fi
exit 0
""",
    # `enable --now` writes the marker `is-enabled` reads back, so the
    # script's post-action re-query (#2082/#2096) is exercised for real.
    # COORD_STUB_ENABLE_IS_A_LIE=1 makes `enable` succeed without the state
    # actually moving — the shape the confirm-after-the-action check exists
    # to catch.
    "systemctl": """
[ "$1" = "--user" ] && shift
units="$COORD_STUB_STATE/units"; mkdir -p "$units"
case "$1" in
    is-enabled)
        if [ -f "$units/$2" ]; then printf 'enabled\\n'; exit 0; fi
        printf 'disabled\\n'; exit 1 ;;
    enable)
        shift
        for a in "$@"; do case "$a" in --now) ;; *) unit="$a" ;; esac; done
        [ "${COORD_STUB_ENABLE_IS_A_LIE:-0}" = "1" ] || : > "$units/$unit"
        exit 0 ;;
    daemon-reload|restart|start|stop) exit 0 ;;
esac
exit 0
""",
}


def _install_agent_stub(real_python: str) -> str:
    """A stand-in for ``install-agent.sh``: the venv + the agent unit.

    Faithful to the three things the real one guarantees and this script
    relies on — an executable ``$VENV/bin/coord`` that answers ``coord
    version``, the ``~/.local/bin/coord`` shim (#2936) that half the packaged
    units ``ExecStart``, and an installed+enabled ``coord-agent.service`` —
    and to nothing else.
    ``COORD_STUB_INSTALL_AGENT_FAIL_ONCE`` makes the first invocation die
    partway (after creating a *broken* venv directory), which is the #2911
    shape the resumability test drives.
    """
    return f"""
venv="${{COORD_PROVISION_VENV_DIR:-$HOME/.coord-venv}}"
units="${{COORD_PROVISION_SYSTEMD_DIR:-$HOME/.config/systemd/user}}"
if [ "${{COORD_STUB_INSTALL_AGENT_FAIL_ONCE:-0}}" = "1" ] \\
   && [ ! -f "$COORD_STUB_STATE/install-agent-attempted" ]; then
    : > "$COORD_STUB_STATE/install-agent-attempted"
    mkdir -p "$venv/bin"          # exists, but has no usable coord (#2911)
    printf 'install-agent.sh: simulated failure\\n' >&2
    exit 1
fi
mkdir -p "$venv/bin" "$units" "$COORD_STUB_STATE/units"
cp "$COORD_STUB_BIN/coord" "$venv/bin/coord"
chmod +x "$venv/bin/coord"
cat > "$venv/bin/python" <<'WRAP'
#!/usr/bin/env bash
exec {real_python} "$@"
WRAP
chmod +x "$venv/bin/python"
printf '[Service]\\nExecStart=%s/bin/coord agent\\n' "$venv" > "$units/coord-agent.service"
: > "$COORD_STUB_STATE/units/coord-agent"
shim="${{COORD_PROVISION_LOCAL_BIN:-$HOME/.local/bin}}"
mkdir -p "$shim"
ln -sf "$venv/bin/coord" "$shim/coord"
exit 0
"""


COORD_STUB = '''
import os, sys, pathlib

log = pathlib.Path(os.environ["COORD_STUB_LOG"])
with log.open("a") as fh:
    fh.write("coord " + " ".join(sys.argv[1:]) + "\\n")

argv = sys.argv[1:]


def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


if argv[:1] == ["version"]:
    print("coord " + os.environ.get("COORD_STUB_VERSION", "1.2.3"))
    raise SystemExit(0)

if argv[:2] == ["machine", "add"]:
    name = argv[2]
    cfg = pathlib.Path(opt("--config"))
    caps = [c for c in (opt("--capabilities") or "").split(",") if c]
    repos = [r for r in (opt("--repos") or "").split(",") if r]
    entry = ["  - name: %s" % name, "    host: %s" % opt("--host")]
    entry.append("    capabilities: [%s]" % ", ".join(caps))
    entry.append("    repos: [%s]" % ", ".join(repos))
    if repos:
        entry.append("    repo_paths:")
        entry += ["      %s: ~/src/%s" % (r, r) for r in repos]
    with cfg.open("a") as fh:
        fh.write("\\n".join(entry) + "\\n")
    print("wrote machines[%s]" % name)
    raise SystemExit(0)

if argv[:2] == ["machine", "doctor"]:
    name = argv[2]
    if os.environ.get("COORD_STUB_DOCTOR_SILENT") == "1":
        raise SystemExit(0)          # exits 0 having graded nothing
    crits = [c for c in os.environ.get("COORD_STUB_DOCTOR_CRIT", "").split(",") if c]
    print("machine: %s" % name)
    for check in crits:
        print("  \\u2717 CRIT [%s] stubbed failure" % check)
    print("")
    print(
        "MACHINE_DOCTOR: machine=%s crit=%d warn=0 unknown=0 ok=%s"
        % (name, len(crits), "false" if crits else "true")
    )
    raise SystemExit(1 if crits else 0)

if argv[:2] == ["repo", "doctor"]:
    raise SystemExit(0)

raise SystemExit(0)
'''


@dataclass
class Box:
    home: Path
    bin: Path
    log: Path
    state: Path
    settings: Path
    env: dict[str, str]
    #: Stands in for the host's own ``/usr/bin`` — on PATH *after* the stub
    #: fleet, so anything written here is reachable only when no stub shadows
    #: it. Empty by default; a test writes into it to model a host that ships
    #: a tool of its own. See ``_browser_box``.
    system: Path

    def run(self, *args: str, stdin: str = "", **env: str) -> subprocess.CompletedProcess:
        merged = {**self.env, **env}
        return subprocess.run(  # noqa: S603
            ["bash", str(SCRIPT), *args],
            capture_output=True, text=True, input=stdin, env=merged, timeout=180,
        )

    @property
    def invocations(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""


@pytest.fixture
def box(tmp_path: Path) -> Box:
    home = tmp_path / "home"
    binder = tmp_path / "bin"
    state = tmp_path / "state"
    system = tmp_path / "system"
    for d in (home, binder, state, system):
        d.mkdir(parents=True)
    log = tmp_path / "invocations.log"
    log.touch()

    def write(name: str, body: str, *, shebang: str = "#!/usr/bin/env bash") -> None:
        path = binder / name
        path.write_text(f"{shebang}\n{_LOGGER}{body}", encoding="utf-8")
        path.chmod(0o755)

    for name, body in STUBS.items():
        write(name, body)
    # `coord` is Python so it can append real YAML the real coord.config then
    # parses on the next run — a bash heredoc would not survive that.
    (binder / "coord").write_text(
        f"#!{sys.executable}\n{COORD_STUB}", encoding="utf-8"
    )
    (binder / "coord").chmod(0o755)
    write("install-agent.sh", _install_agent_stub(sys.executable))

    # python3 must be the REAL interpreter (the script's 3.12 floor check and
    # `-m ensurepip` probe are load-bearing), so it is not stubbed; the stub
    # dir simply does not shadow it.

    settings = home / "src" / "coord-settings"
    (settings / "coord").mkdir(parents=True)
    (settings / ".git").mkdir()
    (settings / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (settings / "coord" / "coordinator.yml").write_text(SETTINGS_CONFIG, encoding="utf-8")

    # Credentials that already exist do not re-prompt — that is what makes an
    # unattended second run possible at all.
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    (home / ".coord").mkdir()
    (home / ".coord" / "serve_token").write_text("pre-existing-token\n", encoding="utf-8")
    (home / ".coord" / "serve_token").chmod(0o600)
    # Deliberately world-readable: the script must tighten it, not just accept it.
    (home / ".coord" / "backup.env").write_text(
        "COORD_BACKUP_REPOSITORY=azure:coordbackup:/\nRESTIC_PASSWORD=already-set\n",
        encoding="utf-8",
    )
    (home / ".coord" / "backup.env").chmod(0o644)

    env = {
        "HOME": str(home),
        # The real /usr/local/bin:/usr/bin:/bin have to stay on the end:
        # python3 must be the genuine interpreter (below), and the script
        # leans on coreutils throughout. That means every name the stub fleet
        # does NOT shadow falls through to whatever this host happens to have
        # installed — so a test that needs a tool to be *absent* must shadow
        # it explicitly rather than assume. `system` sits between the two as
        # a controllable stand-in for that fall-through.
        "PATH": f"{binder}:{system}:/usr/local/bin:/usr/bin:/bin",
        "COORD_STUB_LOG": str(log),
        "COORD_STUB_STATE": str(state),
        "COORD_STUB_BIN": str(binder),
        "COORD_SETTINGS_DIR": str(settings),
        "COORD_PROVISION_INSTALL_AGENT": str(binder / "install-agent.sh"),
        "COORD_PROVISION_ASSUME_YES": "1",
        "COORD_PROVISION_BACKUP_MOUNT": str(tmp_path / "ssd"),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
    }
    (tmp_path / "ssd").mkdir()
    return Box(home=home, bin=binder, log=log, state=state, settings=settings,
               env=env, system=system)


def _ok(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert result.returncode == 0, (
        f"exit {result.returncode}\n--- stdout ---\n{result.stdout}"
        f"\n--- stderr ---\n{result.stderr}"
    )
    return result


WORKER_ARGS = ("--machine", "testbox", "--host", "testbox.tail.ts.net",
               "--repos", "api,shared", "--capabilities", "rust,gtk")


# ── The phase plan, and the ordering invariant #3138 actually names ──────────


@pytest.mark.parametrize("role", ["thin-client", "worker", "server"])
def test_dry_run_prints_the_phase_plan_and_the_gate_for_every_role(box: Box, role: str):
    out = _ok(box.run("--role", role, "--machine", "testbox", "--dry-run")).stdout
    assert "PLAN: role=%s" % role in out
    assert "gate" in out
    assert "coord machine doctor testbox --ssh -v --role" in out
    doctor_role = "daemon" if role == "server" else "worker"
    assert f"--role {doctor_role}" in out


@pytest.mark.parametrize("role", ["thin-client", "worker", "server"])
def test_every_interactive_prompt_happens_in_one_block_before_the_first_slow_phase(
    box: Box, role: str
):
    """#3138's ordering criterion, asserted on the script's own phase table.

    ``tailscale up``, ``gh auth login`` and the Claude OAuth all need a human.
    Scattering them through a 40-minute run is how an operator wanders off and
    comes back to a box that has been blocked on a prompt for half an hour. So:
    exactly ONE interactive phase, and it precedes every slow one.
    """
    out = _ok(box.run("--role", role, "--machine", "testbox", "--dry-run")).stdout
    phases = [
        (line.split()[2], line.split()[3])
        for line in out.splitlines()
        if line.startswith("PLAN: ") and len(line.split()) == 4 and line.split()[1].isdigit()
    ]
    assert phases, out
    speeds = [speed for _, speed in phases]
    assert speeds.count("interactive") == 1, f"prompts are not in ONE block: {phases}"

    interactive_at = speeds.index("interactive")
    slow_at = [i for i, s in enumerate(speeds) if s == "slow"]
    if slow_at:
        assert interactive_at < min(slow_at), (
            f"credentials phase runs at index {interactive_at}, after a slow phase "
            f"at {min(slow_at)}: {phases}"
        )
    # And the gate is unconditionally last, whatever the role.
    assert phases[-1][0] == "gate"


def test_an_unknown_role_is_rejected_rather_than_defaulted(box: Box):
    result = box.run("--role", "daemon", "--machine", "testbox", "--dry-run")
    assert result.returncode != 0
    assert "unknown --role" in result.stderr


def test_a_thin_client_refuses_capabilities_and_repos(box: Box):
    result = box.run("--role", "thin-client", "--machine", "testbox",
                     "--repos", "api", "--dry-run")
    assert result.returncode != 0
    assert "thin-client" in result.stderr


# ── A whole run, per role ────────────────────────────────────────────────────


def test_thin_client_run_ends_on_a_clean_gate(box: Box):
    out = _ok(box.run("--role", "thin-client", "--machine", "testbox",
                      "--host", "testbox.tail.ts.net")).stdout
    assert "PROVISION: role=thin-client machine=testbox" in out
    assert "gate=pass" in out
    # No toolchains, no clones, no daemon units — the point of the role.
    assert "phase" in out and "toolchains" not in out
    assert not (box.home / "src" / "api").exists()
    installed = {p.name for p in (box.home / ".config/systemd/user").glob("*")}
    assert installed == {"coord-agent.service"}


def test_worker_run_clones_repos_installs_toolchains_and_gates(box: Box):
    out = _ok(box.run("--role", "worker", *WORKER_ARGS)).stdout
    assert "gate=pass" in out
    for repo in ("api", "shared"):
        assert (box.home / "src" / repo / ".git").is_dir(), out
    assert "coord repo doctor api --fix" in box.invocations
    # Declared capabilities were already satisfied by the stub toolchain, so
    # the phase must report so rather than reinstalling.
    assert "NOCHANGE: [toolchains]" in out
    installed = {p.name for p in (box.home / ".config/systemd/user").glob("*.service")}
    assert installed == {"coord-agent.service"}, "a worker runs coord-agent only"


def test_server_installs_exactly_the_manifests_units_and_confirms_each_enabled(box: Box):
    """The ten units are read from ``deploy_manifest.ROLE_UNITS``, not from a
    second list in the script — so this fails the day the manifest grows an
    eleventh and nobody touches the installer."""
    out = _ok(box.run("--role", "server", *WORKER_ARGS)).stdout
    assert "gate=pass" in out

    expected = set(units_for_role(ROLE_DAEMON))
    unit_dir = box.home / ".config/systemd/user"
    installed = {p.name for p in unit_dir.glob("*") if p.suffix in (".service", ".timer")}
    assert expected <= installed, f"missing {expected - installed}"

    enabled = {p.name for p in (box.state / "units").glob("*")}
    for unit in expected - {"coord-agent.service"}:
        assert unit in enabled, f"{unit} installed but never enabled"

    # The daemon's own credential + tool, which no other role is asked for.
    backup_env = box.home / ".coord" / "backup.env"
    assert (box.home / ".coord" / "role").read_text().strip() == "daemon"
    assert backup_env.exists() or "backup.env" in out


def test_every_installed_unit_has_something_to_run(box: Box):
    """An `enabled` timer whose companion `.service` is missing, or a service
    whose `ExecStart=` points at a path nothing put there, is the worst shape
    in this whole script: `systemctl --user is-enabled` says `enabled`, the
    unit file looks byte-for-byte right, and every fire dies unread in the
    journal (`Unit not found` / `status=203/EXEC`). That is the #2082 failure
    class, and on a rebuilt dellserver it means the hourly coord.db snapshot
    silently never runs.

    Found by tier 3 of scripts/verify-provision-noble.sh, which resolves the
    same ExecStarts against a real Ubuntu 24.04 rootfs; pinned here so it
    cannot come back without a stub-speed test failing first.
    """
    result = _ok(box.run("--role", "server", *WORKER_ARGS))
    out = result.stdout + result.stderr   # warn() goes to stderr
    unit_dir = box.home / ".config/systemd/user"

    for timer in sorted(unit_dir.glob("*.timer")):
        companion = unit_dir / (timer.name[: -len(".timer")] + ".service")
        assert companion.exists(), (
            f"{timer.name} is installed and enabled but {companion.name} — the "
            f"unit it fires — was never installed, so every fire is a no-op"
        )

    checked = 0
    for service in sorted(unit_dir.glob("*.service")):
        for line in service.read_text(encoding="utf-8").splitlines():
            if not line.startswith("ExecStart="):
                continue
            # systemd's own expansions: %h is the unit owner's home, and a
            # leading -/+/!/: is a modifier, not part of the path.
            command = line[len("ExecStart="):].lstrip("-+!:@").split()[0]
            resolved = Path(command.replace("%h", str(box.home)))
            checked += 1
            if resolved.exists():
                continue
            # It may still be unresolvable: `coord-web-dist-build.sh` is in
            # the repo's deploy/ but NOT in the wheel's coord/deploy/, so a
            # release-only install (no checkout to fall back to — see
            # test_a_dead_execstart_is_advisory_counted_and_never_fatal)
            # cannot produce it. What must never happen is that going unsaid:
            # the run has to name the unit.
            assert f"{service.name} runs {command}" in out, (
                f"{service.name}: ExecStart={command} resolves to {resolved}, "
                f"which this run never created (203/EXEC on every start) — and "
                f"the run said nothing about it"
            )
            assert "203/EXEC" in out, "the warning must say what the symptom looks like"
    assert checked >= len(list(unit_dir.glob("*.service"))), "no ExecStart was checked"


def test_a_helper_the_release_does_not_ship_is_staged_from_this_checkout(box: Box):
    """`coord/deploy/README.md` says the `*.sh` helpers "are not copied here"
    and tests/test_packaged_deploy_units.py pins that only
    ``coord-db-backup.sh`` is — so a daemon host installed purely from the
    wheel has `coord-web-dist-build.service` armed with an `ExecStart=` that
    resolves to nothing. The checkout this script is being run out of is the
    only place that file exists, so it is the fallback.
    """
    out = _ok(box.run("--role", "server", *WORKER_ARGS)).stdout
    local_bin = box.home / ".local" / "bin"

    source = REPO_ROOT / "deploy" / "coord-web-dist-build.sh"
    assert source.is_file(), "the premise moved: deploy/coord-web-dist-build.sh is gone"
    staged = local_bin / source.name
    assert staged.is_file(), (
        "coord-web-dist-build.service's ExecStart names "
        f"%h/.local/bin/{source.name} and nothing put it there"
    )
    assert staged.read_text() == source.read_text()
    assert staged.stat().st_mode & 0o111, "a helper systemd ExecStarts must be executable"
    # and it says WHERE it came from — a checkout-sourced file is not a
    # released artifact and the operator has to be able to tell.
    assert f"{source.name} (from {REPO_ROOT / 'deploy'}" in out
    assert "does not ship it" in out


def test_a_helper_the_release_does_ship_is_never_overridden_by_the_checkout(box: Box):
    """The fallback is last-resort only. If the release packages a helper,
    the release wins — otherwise this would quietly reintroduce exactly the
    #1927 "installed from whatever the checkout happened to hold" drift that
    installing from `packaged_unit_dir()` exists to prevent.
    """
    from coord.health.checks.unit_drift import packaged_unit_dir

    packaged = packaged_unit_dir() / "coord-db-backup.sh"
    assert packaged.is_file(), "the premise moved: the wheel no longer ships this helper"

    out = _ok(box.run("--role", "server", *WORKER_ARGS)).stdout
    staged = box.home / ".local" / "bin" / packaged.name
    assert staged.read_text() == packaged.read_text()
    for line in out.splitlines():
        if packaged.name in line:
            assert "does not ship it" not in line, (
                f"{packaged.name} IS packaged in this release; it must not be "
                f"taken from the checkout: {line}"
            )


def test_a_dead_execstart_is_advisory_counted_and_never_fatal(tmp_path: Path, box: Box):
    """Run the script from a directory with no `deploy/` beside it — i.e. the
    release-only case the fallback above cannot rescue.

    The unresolvable unit is still installed and still enabled, deliberately:
    the missing file is a release-packaging gap the operator cannot fix from
    this host, and refusing to finish `--role server` over it would block a
    rebuild on something outside their control. So it is advisory — but it
    must be *loud* and *counted*, because this is the one failure class that
    `is-enabled` and `coord machine doctor` both read as healthy.
    """
    detached = tmp_path / "detached" / "scripts"
    detached.mkdir(parents=True)
    script = detached / "provision-machine.sh"
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    # #3139: the script sources the shared provisioning core from `lib/` beside
    # it, and hard-fails without it (an image or a host built from half a
    # toolchain list is exactly the drift the core exists to stop). Copy it
    # along — what this test detaches from is the repo-root `deploy/`, not the
    # core.
    core_src = SCRIPT.parent / "lib" / "provision-core.sh"
    (detached / "lib").mkdir()
    (detached / "lib" / "provision-core.sh").write_text(
        core_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert not (tmp_path / "detached" / "deploy").exists()

    result = subprocess.run(  # noqa: S603
        ["bash", str(script), "--role", "server", *WORKER_ARGS],
        capture_output=True, text=True, env=box.env, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout + result.stderr

    assert "DEAD ExecStart: coord-web-dist-build.service" in out
    assert "203/EXEC" in out
    assert re.search(r"PROVISION: .*dead_exec=[1-9]", out), (
        "a dead ExecStart must reach the machine-readable trailer, not just "
        f"scroll past in the phase output:\n{out}"
    )
    assert "the doctor cannot see it" in out
    # ...and the unit really was enabled anyway — that is the tradeoff being
    # asserted, not an accident.
    assert "coord-web-dist-build.timer" in {p.name for p in (box.state / "units").glob("*")}


def test_a_clean_run_reports_no_dead_execstarts(box: Box):
    """The counter must be able to read zero, or it grades nothing."""
    out = _ok(box.run("--role", "server", *WORKER_ARGS)).stdout
    assert "dead_exec=0" in out
    assert "DEAD ExecStart" not in out


def test_the_script_names_no_units_of_its_own(box: Box):
    """One question, one answer (#2098): the unit list has exactly one home.

    A literal ``coord-serve.service`` in the installer would be a second
    inventory that agrees with ``ROLE_UNITS`` today and silently diverges the
    next time either moves.
    """
    # Comments may cite a unit by name (they carry the incident history); it
    # is executable text naming one that would be the second inventory.
    code = "\n".join(
        line for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    for unit in units_for_role(ROLE_DAEMON):
        if unit == "coord-agent.service":
            continue  # install-agent.sh's, and named as the thing NOT to touch
        assert unit not in code, (
            f"{unit} is hardcoded in provision-machine.sh — ask "
            "deploy_manifest.units_for_role() instead"
        )
    assert "units_for_role" in code


# ── Idempotence and resumability ─────────────────────────────────────────────


@pytest.mark.parametrize("role", ["thin-client", "worker", "server"])
def test_a_second_run_changes_nothing_and_still_exits_zero(box: Box, role: str):
    args = ("--role", role, "--machine", "testbox", "--host", "testbox.tail.ts.net")
    if role != "thin-client":
        args = ("--role", role, *WORKER_ARGS)
    _ok(box.run(*args))
    second = _ok(box.run(*args))
    assert "changes=0" in second.stdout, second.stdout
    assert "Nothing changed" in second.stdout
    mutations = [ln for ln in second.stdout.splitlines() if ln.startswith("CHANGE:")]
    assert not mutations, "the second run mutated something:\n" + "\n".join(mutations)


def test_a_second_run_does_not_reinstall_the_venv_or_reclone(box: Box):
    _ok(box.run("--role", "worker", *WORKER_ARGS))
    box.log.write_text("", encoding="utf-8")
    _ok(box.run("--role", "worker", *WORKER_ARGS))
    log = box.invocations
    assert "install-agent.sh" not in log, "re-ran the installer on a whole venv"
    assert "git clone" not in log, "re-cloned an existing checkout"
    assert "apt-get install" not in log
    assert "tailscale up" not in log, "re-prompted for a credential"
    assert "auth login" not in log, "re-prompted for a credential"


def test_resumable_after_the_venv_phase_dies_partway(box: Box):
    """#2911, the trap that cost six hand-found failures onboarding dell64: a
    first run that dies leaves ``~/.coord-venv`` existing but unusable. The
    contract is that you fix the cause and re-run the WHOLE script."""
    first = box.run("--role", "worker", *WORKER_ARGS,
                    COORD_STUB_INSTALL_AGENT_FAIL_ONCE="1")
    assert first.returncode != 0
    assert (box.home / ".coord-venv").exists(), "precondition: a partial venv is left behind"
    assert not (box.home / ".coord-venv" / "bin" / "coord").exists()

    second = _ok(box.run("--role", "worker", *WORKER_ARGS,
                         COORD_STUB_INSTALL_AGENT_FAIL_ONCE="1"))
    assert "gate=pass" in second.stdout


def test_resumable_after_an_interrupted_clone_and_the_partial_is_never_deleted(box: Box):
    """``~/src/<repo>`` is the worker WORKTREE BASE (CLAUDE.md); deleting one
    to "fix" it orphans live worktrees. An interrupted clone is moved aside,
    with the reason printed, and the re-clone then succeeds."""
    stranded = box.home / "src" / "api"
    stranded.mkdir(parents=True)
    (stranded / "half-a-checkout").write_text("interrupted", encoding="utf-8")

    result = _ok(box.run("--role", "worker", *WORKER_ARGS))
    assert "gate=pass" in result.stdout
    assert (stranded / ".git").is_dir(), "the repo was not re-cloned"

    aside = sorted(box.home.glob("src/api.incomplete.*"))
    assert aside, "the interrupted clone was not preserved"
    assert (aside[0] / "half-a-checkout").exists(), "content was destroyed, not moved"


# ── The gate: it must be able to fail ────────────────────────────────────────


def test_a_failing_doctor_exits_non_zero_and_names_the_failing_layer(box: Box):
    result = box.run("--role", "worker", *WORKER_ARGS,
                     COORD_STUB_DOCTOR_CRIT="identity.board_token_missing,clones.missing")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "GATE FAILED" in combined
    assert "identity" in combined and "clones" in combined
    assert "identity.board_token_missing" in combined
    assert "gate=pass" not in combined
    assert "PROVISION:" not in combined, "reported a completed provision over a failed gate"


def test_a_doctor_that_produced_no_report_is_a_failure_not_a_pass(box: Box):
    """#2096: exit 0 only proves the process ended. A ``coord`` too old to
    know ``--role``, or one that died before rendering, exits 0 having graded
    nothing — and the absence of a verdict must never read as a passing one."""
    result = box.run("--role", "worker", *WORKER_ARGS, COORD_STUB_DOCTOR_SILENT="1")
    assert result.returncode != 0
    assert "no MACHINE_DOCTOR: trailer" in result.stderr
    assert "A missing verdict is not a passing verdict" in result.stderr


def test_the_gate_refuses_to_grade_when_its_ssh_probe_cannot_run(box: Box):
    """Without ``--ssh`` the toolchain and identity layers read UNKNOWN — never
    a CRIT — so a gate run blind would exit 0 having checked six of eight
    layers. It must fail instead of reporting a green it did not earn."""
    result = box.run("--role", "worker", *WORKER_ARGS, COORD_STUB_SSH_RC="255")
    assert result.returncode != 0
    assert "cannot ssh" in result.stderr
    assert "UNKNOWN" in result.stderr
    assert "machine doctor" not in box.invocations, (
        "the doctor was run anyway — its verdict would have been a green it "
        "could not have earned without the --ssh probe"
    )


def test_an_enable_that_did_not_take_fails_the_daemon_phase(box: Box):
    """#2082: an installed-but-disabled timer's file is byte-for-byte identical
    to an active one's, which is how ``coord-release-propagate.timer`` sat dead
    on three hosts. ``systemctl enable`` exiting 0 is not the verdict."""
    result = box.run("--role", "server", *WORKER_ARGS, COORD_STUB_ENABLE_IS_A_LIE="1")
    assert result.returncode != 0
    assert "is-enabled" in result.stderr
    assert "#2082" in result.stderr


# ── Credential hygiene ───────────────────────────────────────────────────────


SECRET = "b0ardT0kenb0ardT0kenb0ardT0kenb0ardT0ken"


def test_no_credential_reaches_a_log_line_argv_or_a_non_0600_file(box: Box):
    """The board token is typed at a prompt in a phase that also shells out to
    six other binaries. It must land in exactly one place, mode 0600, and
    appear in no output line and no argv."""
    (box.home / ".coord" / "serve_token").unlink()
    result = _ok(
        box.run(
            "--role", "worker", *WORKER_ARGS,
            # No --yes: exercise the real prompt path. stdin is the
            # confirmation, the (skipped) coordinator key, then the token.
            stdin=f"\n\n{SECRET}\n",
            COORD_PROVISION_ASSUME_YES="0",
        )
    )
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
    assert SECRET not in box.invocations, "a credential reached a subprocess argv"

    token_file = box.home / ".coord" / "serve_token"
    assert token_file.read_text().strip() == SECRET
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    # And nowhere else under $HOME.
    holders = []
    for path in box.home.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            if SECRET in path.read_text(encoding="utf-8", errors="ignore"):
                holders.append(path)
        except OSError:  # pragma: no cover - defensive
            continue
    assert holders == [token_file], holders


def test_every_credential_file_it_touches_ends_mode_0600(box: Box):
    _ok(box.run("--role", "server", *WORKER_ARGS))
    for rel in (".coord/serve_token", ".coord/backup.env",
                ".ssh/id_ed25519", ".claude/.credentials.json"):
        path = box.home / rel
        if not path.exists():
            continue
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, rel


# ── The two properties that separate this from the Azure image builder ───────


def test_it_never_creates_a_second_user_account(box: Box):
    """``provision-worker.sh`` creates a dedicated ``coord`` user because
    ``waagent -deprovision+user`` deletes the provisioning user's home. On
    bare metal the operator IS the user, and a second account would put
    ``~/.coord-venv``, ``~/src`` and ``~/.claude`` where the agent never
    looks."""
    _ok(box.run("--role", "server", *WORKER_ARGS))
    assert "SECOND-USER" not in box.invocations
    assert "useradd" not in box.invocations
    assert "adduser" not in box.invocations
    source = SCRIPT.read_text(encoding="utf-8")
    assert "useradd" not in source
    assert "adduser" not in source


def test_it_refuses_to_run_as_root(box: Box):
    result = box.run("--role", "server", *WORKER_ARGS, COORD_STUB_UID="0")
    assert result.returncode != 0
    assert "refusing to run as root" in result.stderr
    assert "SECOND-USER" not in box.invocations


def test_it_never_requires_azure(box: Box):
    """A machine must be buildable with a laptop and a browser. Neither ``az``
    nor ``waagent`` exists on ``$PATH`` in this harness, so a run that
    completes proves the Azure lane is genuinely optional — the Key Vault
    path is an offer, never a dependency."""
    assert not (box.bin / "az").exists()
    assert not (box.bin / "waagent").exists()
    _ok(box.run("--role", "server", *WORKER_ARGS))
    log = box.invocations
    assert "\naz " not in "\n" + log
    assert "waagent" not in log


def test_the_venv_is_delegated_to_install_agent_and_is_never_editable(box: Box):
    """The standing invariant in ``docs/AGENT_OPERATIONS.md``: ``~/.coord-venv``
    is a PyPI install. An editable one makes ``coord agent update`` git-pull a
    checkout instead of upgrading, so released versions never propagate."""
    code = "\n".join(
        line for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "pip install" not in code, (
        "provision-machine.sh must not install the venv itself — install-agent.sh "
        "owns that layer, including the #2911 partial-venv recovery"
    )
    assert "pip install -e" not in SCRIPT.read_text(encoding="utf-8")
    _ok(box.run("--role", "worker", *WORKER_ARGS))
    assert "install-agent.sh" in box.invocations

    real = "\n".join(
        line for line in (REPO_ROOT / "install-agent.sh").read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "code-coordinator[server]" in real
    assert "pip install -e" not in real
    assert "install --editable" not in real


# ── The role declaration (#3128) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("role", "declared"),
    [("thin-client", "worker"), ("worker", "worker"), ("server", "daemon")],
)
def test_the_role_written_to_disk_is_the_one_the_gate_grades_against(
    box: Box, role: str, declared: str
):
    """The doctor knows exactly two roles (#3128). A host whose ``~/.coord/role``
    said one thing while the gate graded another would be a split-brain in the
    one file that has to resolve with the board down."""
    args = ("--role", role, "--machine", "testbox", "--host", "testbox.tail.ts.net")
    if role != "thin-client":
        args = ("--role", role, *WORKER_ARGS)
    out = _ok(box.run(*args)).stdout
    assert (box.home / ".coord" / "role").read_text().strip() == declared
    assert f"coord machine doctor testbox --ssh -v --role {declared}" in box.invocations
    assert "resolve_role() reads back" in out


def test_the_role_file_is_read_back_through_3128s_resolver(box: Box):
    source = SCRIPT.read_text(encoding="utf-8")
    assert "resolve_role" in source, (
        "the role must be confirmed through deploy_manifest.resolve_role — the only "
        "thing entitled to read ~/.coord/role — not by re-reading the file here"
    )


def test_a_role_file_the_resolver_disagrees_with_stops_the_run(box: Box):
    """The confirm-after-the-action check must be reachable: make the file
    unwritable-as-intended by pre-seeding an env override the resolver honours
    ahead of the file, and the script must refuse rather than carry on
    grading the host as something it did not provision."""
    result = box.run("--role", "server", *WORKER_ARGS, COORD_ROLE="worker")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "resolver reads" in combined
    assert "silently grade this host as a worker" in combined


# ── The backup mount: one path, two scripts, no drift ────────────────────────


def _script_default(name: str) -> str:
    """The literal default of ``NAME="${OVERRIDE:-default}"`` in the script."""
    m = re.search(rf'^{name}="\$\{{[A-Z_]+:-([^}}]*)\}}"', SCRIPT.read_text(encoding="utf-8"),
                  re.MULTILINE)
    assert m, f"could not find a {name}= default in {SCRIPT}"
    return m.group(1)


def test_the_backup_mount_default_is_the_path_coord_db_backup_sh_actually_checks():
    """The unmodified default, pinned — every other test in this file overrides
    ``COORD_PROVISION_BACKUP_MOUNT`` to a tmp dir, so none of them would notice
    a revert to a path that exists nowhere else in the fleet (it was
    ``/mnt/coord-ssd`` once). The assertion is not against a literal typed here
    but against the sibling unit's own default, so the two cannot drift: this
    is the mount ``coord-db-backup.sh`` refuses to write without."""
    backup_sh = (REPO_ROOT / "coord" / "deploy" / "coord-db-backup.sh").read_text(encoding="utf-8")
    m = re.search(r'^DEST_DIR="\$\{COORD_BACKUP_DIR:-([^}]*)\}"', backup_sh, re.MULTILINE)
    assert m, "coord-db-backup.sh no longer declares a default COORD_BACKUP_DIR"
    # coord-db-backup.sh checks `mountpoint -q "$(dirname "$DEST_DIR")"`.
    expected_mount = str(PurePosixPath(m.group(1)).parent)

    assert _script_default("BACKUP_MOUNT") == expected_mount, (
        "provision-machine.sh's BACKUP_MOUNT default and coord-db-backup.sh's own "
        "mount check disagree — --role server would grade a path the backup lane "
        "never writes to"
    )
    assert expected_mount == "/media/crucial", (
        "both moved together, which is fine, but docs/AGENT_OPERATIONS.md's backup "
        "table still says /media/crucial — update it in the same commit"
    )


def test_the_unoverridden_default_mount_is_what_the_server_role_reports_on(box: Box):
    """End to end, with the override REMOVED: the warning a real operator sees
    on a daemon host names the real backup target."""
    # An unmounted SSD is the case the warning exists for, and the case where
    # naming the wrong path is most expensive.
    (box.bin / "mountpoint").write_text(
        f"#!/usr/bin/env bash\n{_LOGGER}exit 1\n", encoding="utf-8")
    (box.bin / "mountpoint").chmod(0o755)
    env = {k: v for k, v in box.env.items() if k != "COORD_PROVISION_BACKUP_MOUNT"}
    result = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT), "--role", "server", *WORKER_ARGS],
        capture_output=True, text=True, input="", env=env, timeout=180,
    )
    _ok(result)
    combined = result.stdout + result.stderr
    assert "/media/crucial is not a mount point" in combined, combined
    assert "/mnt/coord-ssd" not in combined
    assert "mountpoint -q /media/crucial" in box.invocations, box.invocations


# ── The browser capability: a name on PATH is not a browser (#1678) ──────────

# Ubuntu 24.04's real `chromium-browser`, byte-for-byte in behaviour: the deb
# is a 50 KB transitional package whose /usr/bin/chromium-browser refuses to do
# anything without the snap. Verified against the real deb by
# scripts/verify-provision-noble.sh.
NOBLE_CHROMIUM_STUB = """
echo "Command '$0' requires the chromium snap to be installed." >&2
exit 1
"""


# Every name `browser_works()` tries, in its order. Pinned against the script
# by the test below, because this harness has to shadow ALL of them.
#
# It used to shadow only `chromium` (by deleting the working stub), which is
# true of a fleet box and false of a GitHub Actions ubuntu-24.04 runner: that
# image preinstalls BOTH Google Chrome and Chromium into /usr/bin, which this
# fixture's PATH deliberately still reaches (python3 must be real). So the
# probe found a genuine, genuinely-working browser, `browser_works` returned 0
# for real, and the two "there is no browser here" tests below inverted —
# green on every fleet machine and on a laptop, red in every full-suite CI job.
BROWSER_PROBE_NAMES = ("chromium", "google-chrome", "google-chrome-stable",
                       "chromium-browser")

# On PATH, executable, and no more a browser than a missing file is. Shadowing
# with this rather than deleting is what makes "absent" mean absent regardless
# of what the host underneath has installed.
_NOT_A_BROWSER = "exit 127\n"


def test_the_harness_pins_every_browser_name_the_script_probes():
    """If `browser_works()` grows a candidate, `_browser_box` must shadow it
    too — otherwise the host's own copy of that browser silently answers the
    probe and the negative tests below stop testing anything."""
    m = re.search(r"^browser_works\(\)\s*\{.*?^\s*for bin in ([^;]+); do",
                  SCRIPT.read_text(encoding="utf-8"), re.M | re.S)
    assert m, "could not find browser_works()'s candidate loop"
    assert tuple(m.group(1).split()) == BROWSER_PROBE_NAMES


def _browser_box(box: Box, *, stub: bool, snap: str | None) -> Box:
    """Reshape the stub fleet for the browser capability. The default fleet has
    a working `chromium`, which is the case that never needed fixing.

    Shadows every candidate in `BROWSER_PROBE_NAMES` so "no working browser"
    holds on a host that ships one of its own (see that constant)."""
    for name in BROWSER_PROBE_NAMES:
        _restub(box, name, _NOT_A_BROWSER)
    if stub:
        _restub(box, "chromium-browser", NOBLE_CHROMIUM_STUB)
    if snap is not None:
        _restub(box, "snap", snap)
    return box


BROWSER_ARGS = ("--machine", "testbox", "--host", "testbox.tail.ts.net",
                "--repos", "api", "--capabilities", "browser")


def test_a_chromium_browser_that_is_only_a_name_on_path_is_not_a_browser(box: Box):
    """The #1678 false green, as a test. On noble `apt-get install
    chromium-browser` exits 0 and puts a binary on PATH that can never launch;
    the old presence check reported a browser and the machine then advertised a
    capability it could not honour. Nothing must accept that."""
    # The installer reports success and produces nothing runnable — which is
    # what apt genuinely does on noble, and what snapd does on a box where the
    # snap is confined out of existence. Either way the stub is all that is on
    # PATH afterwards, and that must not be mistaken for a browser.
    _browser_box(box, stub=True, snap="exit 0\n")
    result = box.run("--role", "worker", *BROWSER_ARGS)
    assert result.returncode != 0, result.stdout
    combined = result.stdout + result.stderr
    assert "no browser answers --version" in combined, combined
    assert "#1678" in combined
    assert "NOCHANGE: [toolchains] browser" not in combined
    assert "installed a browser" not in combined


def test_the_browser_capability_installs_the_snap_rather_than_the_apt_stub(box: Box):
    """On noble the snap is the only packaging that yields a runnable chromium
    (`apt-cache policy chromium` -> Candidate: (none)), so a host with snap
    must not be sent down the apt path at all."""
    working = box.bin / "chromium"
    snap_stub = (
        'if [ "$1" = install ]; then\n'
        f'  printf "#!/bin/sh\\nexit 0\\n" > "{working}"\n'
        f'  chmod 0755 "{working}"\n'
        "fi\nexit 0\n"
    )
    _browser_box(box, stub=True, snap=snap_stub)
    out = _ok(box.run("--role", "worker", *BROWSER_ARGS)).stdout
    assert "installed a browser" in out, out
    log = box.invocations
    assert "snap install chromium" in log, log
    assert "apt-get install" not in log or "chromium" not in log.split("apt-get install")[-1]


def test_a_working_browser_is_left_alone_and_the_stub_never_shadows_it(box: Box):
    """Both names present, only one runnable: the runnable one must win, and
    the phase must change nothing."""
    _browser_box(box, stub=True, snap=None)
    working = box.bin / "chromium"
    working.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    working.chmod(0o755)
    out = _ok(box.run("--role", "worker", *BROWSER_ARGS)).stdout
    assert "NOCHANGE: [toolchains] browser: chromium" in out, out
    assert "snap" not in box.invocations


@pytest.mark.parametrize("preinstalled", BROWSER_PROBE_NAMES)
def test_a_browser_the_host_ships_cannot_leak_into_the_no_browser_case(
    box: Box, preinstalled: str
):
    """The harness's own isolation, as a test — this is the CI failure that
    made it necessary.

    `box.system` models the host's `/usr/bin`: on PATH, but behind the stub
    fleet. A GitHub Actions ubuntu-24.04 runner ships a working Google Chrome
    AND Chromium there, so before `_browser_box` shadowed every candidate the
    negative browser tests quietly asserted the opposite of what they read —
    passing on a fleet box, failing in CI. Parametrised over every candidate
    so no single name can regress the shadowing on its own."""
    host_browser = box.system / preinstalled
    host_browser.write_text(
        "#!/usr/bin/env bash\nprintf 'Chromium 151.0.7922.0\\n'\nexit 0\n", encoding="utf-8"
    )
    host_browser.chmod(0o755)

    _browser_box(box, stub=True, snap="exit 0\n")
    result = box.run("--role", "worker", *BROWSER_ARGS)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "no browser answers --version" in combined, combined
    # The host's browser must not have been consulted, let alone believed.
    assert "151.0.7922.0" not in combined, combined
    assert "NOCHANGE: [toolchains] browser" not in combined, combined


# ── A failed install reaches the operator as THIS script's message ───────────
#
# Under `set -euo pipefail` a bare `sudo apt-get install ...` that fails aborts
# the script instantly with apt's own error and nothing else — no phase name,
# no "safe to re-run", no hint about which apt source to look at. Every install
# call is therefore written `cmd || die "..."`. These pin that: the stub fleet's
# `apt-get` exits 0 for everything by default, so a deliberately failing one is
# the only way any of these paths is exercised at all.

_FAILING_APT = """
case "$1" in
    update) exit 0 ;;
esac
printf 'E: Unable to locate package (stub)\\n' >&2
exit 100
"""


def _restub(box: Box, name: str, body: str) -> None:
    """Replace one stub in an existing box, keeping the argv logger."""
    path = box.bin / name
    path.write_text(f"#!/usr/bin/env bash\n{_LOGGER}{body}", encoding="utf-8")
    path.chmod(0o755)


def _real_python3() -> str:
    import shutil

    found = shutil.which("python3", path="/usr/local/bin:/usr/bin:/bin")
    assert found, "no system python3 to wrap"
    return found


def test_a_failed_base_package_install_names_the_packages_and_says_re_run(box: Box):
    """phase_base_packages. The probe that fails here is `python3 -m ensurepip
    --version` — the #2911 trap itself — so the missing package is python3-venv,
    the one whose absence bricks the venv layer three phases later."""
    _restub(box, "python3", f"""
if [ "$1" = "-m" ] && [ "$2" = "ensurepip" ]; then
    printf 'No module named ensurepip\\n' >&2
    exit 1
fi
exec {_real_python3()} "$@"
""")
    _restub(box, "apt-get", _FAILING_APT)

    result = box.run("--role", "thin-client", "--machine", "testbox",
                     "--host", "testbox.tail.ts.net")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "apt-get install failed for: python3-venv" in combined, combined
    assert "safe to re-run from the top" in combined, combined
    # It must not have carried on into the credential prompts on a box whose
    # base layer is broken.
    assert "coord machine doctor" not in box.invocations


def test_a_failed_gh_install_names_the_github_cli_source_not_just_apt(box: Box):
    """phase_cred_tools. gh is installed from a THIRD-PARTY apt source, so
    "check apt sources" has to point at that source specifically — a stock
    Ubuntu mirror being fine tells the operator nothing about why this failed."""
    _restub(box, "gh", 'printf "gh version 2.10.0 (stub)\\n"\nexit 0\n')
    _restub(box, "apt-get", _FAILING_APT)
    # The keyring/source-list writes are root-owned paths the transparent
    # `sudo` stub cannot satisfy in a test HOME; stub the three coreutils they
    # use so the failure under test is apt's, not a permission error.
    for tool in ("dd", "tee"):          # these two are piped into
        _restub(box, tool, "cat >/dev/null 2>&1 || true\nexit 0\n")
    for tool in ("install", "chmod"):   # these two are not
        _restub(box, tool, "exit 0\n")

    result = box.run("--role", "worker", *WORKER_ARGS)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "apt-get install gh failed" in combined, combined
    assert "github-cli apt source" in combined, combined


def test_a_failed_gtk_install_is_the_scripts_message_not_a_raw_apt_abort(box: Box):
    """phase_toolchains, the `gtk` capability."""
    _restub(box, "pkg-config", "exit 1\n")   # gtk4 is not visible
    _restub(box, "apt-get", _FAILING_APT)

    result = box.run("--role", "worker", "--machine", "testbox",
                     "--host", "testbox.tail.ts.net",
                     "--repos", "api", "--capabilities", "gtk")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "apt-get install libgtk-4-dev failed" in combined, combined
    assert "safe to re-run from the top" in combined, combined


def test_a_failed_snap_install_explains_why_apt_is_not_the_fallback(box: Box):
    """phase_toolchains, the `browser` capability, on a host that HAS snap.
    The die text has to carry the noble-specific reason, because the obvious
    next move — "just apt-get install chromium" — cannot work there (#1678)."""
    _browser_box(box, stub=True, snap="exit 1\n")   # snapd present but failing

    result = box.run("--role", "worker", *BROWSER_ARGS)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "snap install chromium failed" in combined, combined
    assert "apt-cache policy chromium" in combined, combined
    assert "apt-get install" not in box.invocations


# ── Argument parsing ────────────────────────────────────────────────────────


@pytest.mark.parametrize("flag", ["--role", "--machine", "--host",
                                  "--capabilities", "--repos", "--port"])
def test_a_value_taking_flag_with_no_value_gets_the_usage_not_a_bash_error(
    box: Box, flag: str
):
    """`shift 2` with one positional left fails outright, and under `set -e`
    that aborts with bash's own "shift count out of range" — which names
    neither the flag nor what it wanted."""
    result = box.run(flag)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert f"{flag} requires a value" in combined, combined
    assert "shift count out of range" not in combined, combined
    assert "usage: provision-machine.sh" in combined, combined


# ── The real-surface harness (scripts/verify-provision-noble.sh) ─────────────


NOBLE_HARNESS = REPO_ROOT / "scripts" / "verify-provision-noble.sh"


def test_the_noble_harness_derives_its_inventory_from_the_script_not_a_copy():
    """The harness answers the one question the stub fleet above cannot — "do
    these package names resolve on the OS this targets?" — so it must read the
    package list and the gh floor OUT of provision-machine.sh. A second, hand-
    maintained copy would drift and quietly stop verifying the real thing."""
    assert NOBLE_HARNESS.exists() and os.access(NOBLE_HARNESS, os.X_OK)
    text = NOBLE_HARNESS.read_text(encoding="utf-8")
    assert "BASE_REQUIREMENTS" in text and "GH_MIN_VERSION" in text
    assert "provision-machine.sh" in text
    # #2096: a machine-readable verdict, so nobody has to eyeball a wall of
    # output to know whether it passed.
    assert "NOBLE_VERIFY: ok=" in text


def test_the_noble_harness_actually_drives_the_live_seams_it_names():
    """Tier 2 exists because the previous version of this harness could only
    say it did NOT cover /health and /board. Saying so is not covering them:
    the script must start the real daemons and make the real requests."""
    text = NOBLE_HARNESS.read_text(encoding="utf-8")
    for live in ("coord agent", "coord serve", "/health", "/healthz", "/board",
                 "coord status", "coord machine doctor", "MACHINE_DOCTOR: "):
        assert live in text, f"tier 2 must actually exercise {live}"
    # The private netns is the safety property, not a detail: without it this
    # tier would talk to (and be graded against) the fleet agent on whatever
    # host the harness is run from.
    assert "--net" in text and "ip link set lo up" in text


def test_the_noble_harness_parses_the_daemon_units_with_real_systemd():
    """Tier 3. `systemd-analyze verify` is a static parser — it needs no PID 1
    — so the ten units CAN be checked here even though they cannot be
    started. Rendering must go through the same call phase_daemon_units uses,
    or this grades a hand-copied approximation of the shipped units."""
    text = NOBLE_HARNESS.read_text(encoding="utf-8")
    assert "systemd-analyze verify" in text
    for shared in ("units_for_role", "ROLE_DAEMON", "render_unit", "packaged_unit_dir"):
        assert shared in text, f"tier 3 must render through {shared}"
    assert "ExecStart" in text


def test_the_noble_harness_is_honest_about_what_it_still_cannot_reach():
    """#2096. Three tiers of real coverage make it MORE tempting, not less, to
    read a green here as the throwaway-VM run. The header must keep naming the
    surfaces that are structurally out of a chroot's reach."""
    text = NOBLE_HARNESS.read_text(encoding="utf-8")
    for uncovered in ("tailscale up", "gh auth login", "snap", "PID 1",
                      "necessary, not sufficient"):
        assert uncovered in text, f"the harness must say it cannot cover {uncovered}"


def test_the_noble_harness_verifies_the_rootfs_it_downloads():
    """The tarball is the trust root of every result the harness reports, and
    it is cached across runs — so "it came over https once" is not enough."""
    text = NOBLE_HARNESS.read_text(encoding="utf-8")
    assert "SHA256SUMS" in text and "sha256sum" in text
    assert "--no-checksum" in text, "there must be a knowing opt-out, not a silent skip"


# ── Documentation seam ───────────────────────────────────────────────────────


def test_the_script_points_at_the_runbooks_it_deliberately_does_not_duplicate():
    source = SCRIPT.read_text(encoding="utf-8")
    for pointer in ("docs/MAC_MINI.md", "docs/WSL_WINDOWS_WORKER.md",
                    "docs/DISASTER_RECOVERY.md", "docs/GRAPHIFY_SETUP.md"):
        assert pointer in source, f"{pointer} is out of scope but must be pointed at"


def test_agent_operations_documents_the_installer():
    doc = (REPO_ROOT / "docs" / "AGENT_OPERATIONS.md").read_text(encoding="utf-8")
    assert "provision-machine.sh" in doc
    assert "--role thin-client" in doc


def test_agent_operations_hands_the_operator_the_run_no_harness_can_do():
    """The three verification tiers all run unprivileged, which is exactly why
    it is tempting to stop there. The one layer that needs a hypervisor and a
    human at a browser has to be written down as a runnable checklist, not
    left as "someone should do a VM run": what to launch, what to run, and
    which output to paste back.
    """
    doc = (REPO_ROOT / "docs" / "AGENT_OPERATIONS.md").read_text(encoding="utf-8")
    for needed in (
        "multipass launch 24.04",          # how to get the VM
        "COORD_SETTINGS_DIR=~/scratch-settings",  # don't register a throwaway for real
        "coord machine doctor coordvm --ssh -v",  # what proves the gate
        "localhost:7435/board",           # the server role's own evidence
        "multipass delete --purge",       # and put it away afterwards
    ):
        assert needed in doc, f"the VM checklist must say: {needed}"
    for role in ("thin-client", "worker", "server"):
        assert f"--role {role}      --machine coordvm" in doc or \
               f"--role {role} --machine coordvm" in doc, (
            f"the checklist must run --role {role} on the throwaway VM"
        )
