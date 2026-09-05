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
being restated as a unit test against an extracted helper. The one thing
these cannot cover is the throwaway-VM verification the issue also asks for
(real apt, real tailscale login, a real agent taking a dispatch); that is in
the PR's SMOKE_TESTS block, not here.

The daemon-unit phase is the exception that proves the rule: it runs the
**real** ``coord.deploy_manifest`` / ``coord.deploy_units`` /
``unit_drift.packaged_unit_dir``, through the stub venv's Python, against the
repo's own ``coord/deploy/``. So `test_server_installs_exactly_the_manifests_units`
fails if ``ROLE_UNITS[daemon]`` and the packaged unit set ever disagree.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

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

    Faithful to the two things the real one guarantees and this script relies
    on — an executable ``$VENV/bin/coord`` that answers ``coord version``, and
    an installed+enabled ``coord-agent.service`` — and to nothing else.
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
    for d in (home, binder, state):
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
        "PATH": f"{binder}:/usr/local/bin:/usr/bin:/bin",
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
    return Box(home=home, bin=binder, log=log, state=state, settings=settings, env=env)


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
