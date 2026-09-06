"""Black-box tests for ``scripts/azure-workers/throwaway-vm.sh`` (#3151).

#3138 merged with its own acceptance criterion unmet: run
``provision-machine.sh`` against a real, fresh Ubuntu 24.04 VM. A worker
session structurally cannot do that (no root, no docker/podman/multipass/
qemu), so this script is the mechanical tool that turns "someone should run a
throwaway VM" into a single operator command. It creates a stock Azure VM,
stages ``provision-machine.sh`` onto it, runs it (interactively, for the
credentials phase), captures the evidence, and always tears the VM down.

Real ``az``/``ssh``/``scp`` calls cost money and need a subscription, so — the
same shape as ``tests/test_dr_azure_scripts.py`` and
``tests/test_provision_machine.py`` — these tests drive the REAL script
end-to-end against a ``$PATH`` of small recording/stub binaries. Every
assertion is on the script's actual behaviour (the ``az``/``ssh`` calls it
made, its exit code, what it wrote to the evidence file), never on its source
text, except for the two places where the source text itself is the claim
under test (never reading ``SOURCE_IMAGE_ID``/``epic.env``, and ``bash -n``).
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "azure-workers" / "throwaway-vm.sh"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="throwaway-vm.sh is Ubuntu/az-CLI-oriented and untested elsewhere"
)


# ── stub fleet ───────────────────────────────────────────────────────────────
#
# Every stub appends its own argv to a log file, so tests assert on what the
# script actually invoked rather than trusting its exit code alone (#2096:
# "an observation taken after the action", applied to the test harness too).

_AZ_STUB = r"""#!/usr/bin/env python3
import os, sys

argv = sys.argv[1:]
with open(os.environ["AZ_LOG"], "a") as fh:
    fh.write(" ".join(argv) + "\n")
joined = " ".join(argv)

if "account show" in joined:
    print("test-sub\tsub-id"); sys.exit(0)
if "group show" in joined:
    sys.exit(0 if os.environ.get("STUB_GROUP_EXISTS") == "1" else 1)
if "group create" in joined:
    sys.exit(0)
if "vm create" in joined:
    sys.exit(int(os.environ.get("STUB_VM_CREATE_RC", "0")))
if "vm show" in joined and "-d" in argv:
    print(os.environ.get("STUB_VM_IP", "10.0.0.5")); sys.exit(0)
if "vm show" in joined:
    print("")  # NIC id lookup -- empty forces the <vm>NSG fallback name
    sys.exit(0)
if "network nic show" in joined:
    print(""); sys.exit(0)
if "network nsg rule create" in joined:
    sys.exit(0)
if "vm list" in joined:
    print(os.environ.get("STUB_EXISTING_VM_NAME", "throwaway")); sys.exit(0)
if "group delete" in joined:
    rc = int(os.environ.get("STUB_GROUP_DELETE_RC", "0"))
    if rc != 0:
        sys.stderr.write(os.environ.get(
            "STUB_GROUP_DELETE_STDERR",
            "ERROR: (ResourceGroupBeingDeleted) a lock is present\n",
        ))
    sys.exit(rc)
sys.exit(0)
"""

_CURL_STUB = r"""#!/usr/bin/env python3
import sys
if sys.argv and "api.ipify.org" in sys.argv[-1]:
    print("203.0.113.7")
else:
    print("{}")
sys.exit(0)
"""

# The `ssh`/`scp` stand-in. `throwaway-vm.sh` calls `ssh` for: the plain
# reachability probe (`... user@ip true`), staging (`mkdir`, `chmod`), the
# interactive `-t` provisioning run, and the non-interactive evidence
# commands (`coord machine doctor`, `coord status`, the two curls run ON the
# VM). All of it is one command string, always the LAST argv element.
_SSH_STUB = r"""#!/usr/bin/env python3
import os, sys, time

args = sys.argv[1:]
with open(os.environ["SSH_LOG"], "a") as fh:
    fh.write(" ".join(args) + "\n")
cmd = args[-1] if args else ""

delay = float(os.environ.get("STUB_SSH_DELAY", "0"))
if delay:
    time.sleep(delay)

if cmd == "true":
    sys.exit(int(os.environ.get("STUB_SSH_TRUE_RC", "0")))
if "provision-machine.sh --role" in cmd:
    out = os.environ.get(
        "STUB_PROVISION_OUTPUT",
        "PROVISION: role=worker machine=m phases=8 changes=3 dead_exec=0 gate=pass\n"
        "MACHINE_DOCTOR: machine=m crit=0 warn=0 unknown=0 ok=true",
    )
    print(out)
    sys.exit(int(os.environ.get("STUB_PROVISION_RC", "0")))
if "coord machine doctor" in cmd:
    print(os.environ.get("STUB_DOCTOR_RERUN", "MACHINE_DOCTOR: machine=m crit=0 warn=0 unknown=0 ok=true"))
    sys.exit(0)
if "coord status" in cmd:
    print("machines: 1 idle"); sys.exit(0)
if "7433/health" in cmd:
    print('{"machine":"m","ok":true}'); sys.exit(0)
if "7435/board" in cmd:
    print('{"machines":[]}'); sys.exit(0)
sys.exit(0)
"""


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def stubs(tmp_path: Path) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_stub(bindir / "az", _AZ_STUB)
    _write_stub(bindir / "curl", _CURL_STUB)
    _write_stub(bindir / "ssh", _SSH_STUB)
    _write_stub(bindir / "scp", _SSH_STUB)  # same logger shape; scp's argv is never inspected

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "AZ_LOG": str(tmp_path / "az.log"),
            "SSH_LOG": str(tmp_path / "ssh.log"),
            "HOME": str(tmp_path),  # never touch the real operator's ~/.ssh, ~/.coord etc.
            # Fast by default; individual tests override to exercise the loop.
            "THROWAWAY_VM_SSH_WAIT_ATTEMPTS": "3",
            "THROWAWAY_VM_SSH_WAIT_SLEEP": "0",
        }
    )
    return {
        "env": env,
        "tmp": tmp_path,
        "az_log": tmp_path / "az.log",
        "ssh_log": tmp_path / "ssh.log",
    }


def _az_calls(stubs: dict) -> list[str]:
    log = stubs["az_log"]
    return log.read_text().splitlines() if log.exists() else []


def _ssh_calls(stubs: dict) -> list[str]:
    log = stubs["ssh_log"]
    return log.read_text().splitlines() if log.exists() else []


def _run(stubs: dict, *args: str, env_overrides: dict | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    env = dict(stubs["env"])
    env.update(env_overrides or {})
    out = stubs["tmp"] / f"evidence-{len(list(stubs['tmp'].glob('evidence-*')))}.log"
    return subprocess.run(
        [str(SCRIPT), *args, "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
        cwd=str(stubs["tmp"]),
    )


# ===========================================================================
# 0. syntax + the anti-regression named tests the issue calls out explicitly
# ===========================================================================


def test_bash_syntax_is_clean() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_vm_create_uses_the_stock_ubuntu_sku_never_source_image_id(stubs: dict) -> None:
    """The whole point of this script: verifying against the golden gallery
    image (SOURCE_IMAGE_ID) would prove nothing, because the claim under test
    IS the fresh-OS path. This is the regression that would silently void
    every future run, so it is pinned by name."""
    result = _run(stubs, "--role", "worker", "--rg", "rg-t1")
    assert result.returncode == 0, result.stdout + result.stderr

    calls = _az_calls(stubs)
    create_calls = [c for c in calls if c.startswith("vm create")]
    assert len(create_calls) == 1, calls
    assert "--image Canonical:ubuntu-24_04-lts:server:latest" in create_calls[0]
    assert "SOURCE_IMAGE_ID" not in create_calls[0]
    assert "gallery" not in create_calls[0].lower()

    # And not merely "this run happened not to use it" -- the script must not
    # even be CAPABLE of reading it. Comments are allowed to explain why (and
    # do); only actual code lines are checked.
    code_lines = [line for line in SCRIPT.read_text().splitlines() if not line.strip().startswith("#")]
    code = "\n".join(code_lines)
    for needle in ("$SOURCE_IMAGE_ID", "${SOURCE_IMAGE_ID", "EPIC_ENV", "epic.env"):
        assert needle not in code, f"{needle!r} must never be read by this script's code"


def test_nsg_rule_is_scoped_to_a_single_source_ip_not_star(stubs: dict) -> None:
    result = _run(stubs, "--role", "worker", "--rg", "rg-t2")
    assert result.returncode == 0, result.stdout + result.stderr

    calls = _az_calls(stubs)
    nsg_calls = [c for c in calls if c.startswith("network nsg rule create")]
    assert len(nsg_calls) == 1, calls
    assert "--source-address-prefixes 203.0.113.7/32" in nsg_calls[0]
    assert "--source-address-prefixes *" not in nsg_calls[0]
    assert "0.0.0.0/0" not in nsg_calls[0]


def test_teardown_fires_on_the_failure_path_not_just_success(stubs: dict) -> None:
    """A missing trailer must fail the run AND still delete the resource
    group -- a leaked VM on a failed run is worse than one on a successful
    run, since failed runs are the ones most likely to be re-tried blind."""
    result = _run(
        stubs,
        "--role",
        "worker",
        "--rg",
        "rg-t3",
        env_overrides={
            "STUB_PROVISION_OUTPUT": "PROVISION: role=worker machine=m phases=8 changes=3 dead_exec=0 gate=pass",
        },
    )
    assert result.returncode != 0, result.stdout + result.stderr
    calls = _az_calls(stubs)
    assert any(c.startswith("group delete") for c in calls), calls


def test_absent_machine_doctor_trailer_is_fatal(stubs: dict) -> None:
    """#2096: an absent verdict is a failure, not a pass. provision-machine.sh
    treats a missing MACHINE_DOCTOR trailer as fatal in its own gate; this
    wrapper must not soften that on the way back out, even though the ssh
    session it ran happened to exit 0."""
    result = _run(
        stubs,
        "--role",
        "worker",
        "--rg",
        "rg-t4",
        env_overrides={
            "STUB_PROVISION_OUTPUT": "PROVISION: role=worker machine=m phases=8 changes=3 dead_exec=0 gate=pass",
            "STUB_PROVISION_RC": "0",
        },
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "MACHINE_DOCTOR" in (result.stdout + result.stderr)
    # Fail closed, and still tore down.
    assert any(c.startswith("group delete") for c in _az_calls(stubs))


def test_absent_provision_trailer_is_fatal_even_if_doctor_trailer_is_present(stubs: dict) -> None:
    """A doctor trailer can appear mid-run (phase_gate prints it before
    deciding whether to die) even though the gate itself failed and the
    script died before printing its own PROVISION trailer. An OK doctor
    trailer paired with a MISSING provision trailer (simulating some other
    failure between the gate and the script's own end) must also be fatal —
    checking only the doctor trailer would be trivially foolable."""
    result = _run(
        stubs,
        "--role",
        "worker",
        "--rg",
        "rg-t4b",
        env_overrides={
            "STUB_PROVISION_OUTPUT": "MACHINE_DOCTOR: machine=m crit=0 warn=0 unknown=0 ok=true",
        },
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "PROVISION" in (result.stdout + result.stderr)


def test_unreachable_vm_refuses_non_zero_and_still_tears_down(stubs: dict) -> None:
    result = _run(
        stubs,
        "--role",
        "thin-client",
        "--rg",
        "rg-t5",
        env_overrides={"STUB_SSH_TRUE_RC": "255"},
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "never became reachable" in (result.stdout + result.stderr)
    assert any(c.startswith("group delete") for c in _az_calls(stubs))
    # It must have given up after the configured attempt count, not hung.
    true_calls = [c for c in _ssh_calls(stubs) if c.endswith(" true")]
    assert len(true_calls) == 3  # THROWAWAY_VM_SSH_WAIT_ATTEMPTS from the fixture


def test_teardown_fires_on_interrupt(stubs: dict) -> None:
    """Ctrl-C mid-run must still delete the resource group. Simulated by
    sending SIGINT to the whole process group (matching a real terminal,
    which delivers the signal to every foreground process, not just the
    parent script) while the script is blocked in its SSH-reachability wait."""
    env = dict(stubs["env"])
    env.update(
        {
            "THROWAWAY_VM_SSH_WAIT_ATTEMPTS": "50",
            "THROWAWAY_VM_SSH_WAIT_SLEEP": "1",
            "STUB_SSH_TRUE_RC": "255",
        }
    )
    out = stubs["tmp"] / "evidence-int.log"
    proc = subprocess.Popen(
        [str(SCRIPT), "--role", "server", "--rg", "rg-t6", "--out", str(out)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        cwd=str(stubs["tmp"]),
        start_new_session=True,
    )
    try:
        time.sleep(1.0)  # let it create the RG + VM and enter the wait loop
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        stdout, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.communicate(timeout=5)
        pytest.fail("script did not exit after SIGINT")

    assert proc.returncode != 0, stdout
    calls = _az_calls(stubs)
    assert any(c.startswith("group delete") for c in calls), (stdout, calls)


def test_success_message_says_submitted_not_accepted(stubs: dict) -> None:
    """#3154: '--no-wait' means 'submitted' is the strongest claim the script
    can make at that moment -- not 'accepted', which implies more than a
    fire-and-forget async call proves."""
    result = _run(stubs, "--role", "worker", "--rg", "rg-msg")
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "delete submitted" in combined
    assert "accepted" not in combined.lower()


def test_keep_flag_short_circuits_teardown_unchanged(stubs: dict) -> None:
    """Regression pin for the one branch #3154 must NOT touch: --keep still
    leaves the resource group standing and never calls 'az group delete'."""
    result = _run(stubs, "--role", "worker", "--rg", "rg-keep-explicit", "--keep")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(c.startswith("group delete") for c in _az_calls(stubs))
    assert "leaving rg-keep-explicit standing" in (result.stdout + result.stderr)


def test_failed_group_delete_warns_loudly_with_manual_command_and_flips_exit_status(stubs: dict) -> None:
    """#3154: a submission failure must not look identical to success. It
    must (a) print a WARNING: naming the RG and a copy-pasteable manual
    delete command, (b) surface az's own stderr instead of swallowing it,
    and (c) flip an otherwise-clean run's exit status to non-zero."""
    result = _run(
        stubs,
        "--role",
        "worker",
        "--rg",
        "rg-teardown-fail",
        env_overrides={
            "STUB_GROUP_DELETE_RC": "1",
            "STUB_GROUP_DELETE_STDERR": "ERROR: lock 'do-not-delete' blocks this operation\n",
        },
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "WARNING" in result.stderr
    assert "rg-teardown-fail" in result.stderr
    assert "az group delete -n rg-teardown-fail --yes" in result.stderr
    # az's own stderr reached the operator instead of going to /dev/null.
    assert "ERROR: lock 'do-not-delete' blocks this operation" in result.stderr


def test_failed_group_delete_does_not_mask_an_earlier_failure_exit_code(stubs: dict) -> None:
    """A teardown-submission failure piled onto an already-failed run must
    still warn, but must not be needed to make the run non-zero -- the
    earlier failure's own exit status is not clobbered."""
    result = _run(
        stubs,
        "--role",
        "worker",
        "--rg",
        "rg-teardown-fail-2",
        env_overrides={
            "STUB_PROVISION_OUTPUT": "PROVISION: role=worker machine=m phases=8 changes=3 dead_exec=0 gate=pass",
            "STUB_GROUP_DELETE_RC": "1",
        },
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "WARNING" in result.stderr
    assert "MACHINE_DOCTOR" in (result.stdout + result.stderr)


def test_wait_teardown_confirms_deletion(stubs: dict) -> None:
    """--wait-teardown polls 'az group show' after the delete and reports a
    confirmed deletion once it 404s (the default stub state: no
    STUB_GROUP_EXISTS means the group is already gone)."""
    result = _run(
        stubs,
        "--role",
        "worker",
        "--rg",
        "rg-wait-ok",
        "--wait-teardown",
        env_overrides={
            "THROWAWAY_VM_TEARDOWN_WAIT_ATTEMPTS": "3",
            "THROWAWAY_VM_TEARDOWN_WAIT_SLEEP": "0",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "confirmed" in combined.lower()
    assert "rg-wait-ok" in combined


def test_wait_teardown_timeout_warns_loudly_and_fails(stubs: dict) -> None:
    """If the resource group never 404s within the bounded timeout,
    --wait-teardown must warn loudly and make the run non-zero -- a timeout
    is not silently swallowed either."""
    result = _run(
        stubs,
        "--role",
        "worker",
        "--rg",
        "rg-wait-timeout",
        "--wait-teardown",
        env_overrides={
            "STUB_GROUP_EXISTS": "1",
            "THROWAWAY_VM_TEARDOWN_WAIT_ATTEMPTS": "2",
            "THROWAWAY_VM_TEARDOWN_WAIT_SLEEP": "0",
        },
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "WARNING" in result.stderr
    assert "timed out" in result.stderr.lower()
    assert "rg-wait-timeout" in result.stderr


# ===========================================================================
# 1. per-role behaviour
# ===========================================================================


@pytest.mark.parametrize("role,doctor_role", [("thin-client", "worker"), ("worker", "worker"), ("server", "daemon")])
def test_each_role_runs_against_its_own_fresh_vm(stubs: dict, role: str, doctor_role: str) -> None:
    result = _run(
        stubs,
        "--role",
        role,
        "--rg",
        f"rg-role-{role}",
        env_overrides={
            "STUB_PROVISION_OUTPUT": (
                f"PROVISION: role={role} machine=m phases=8 changes=3 dead_exec=0 gate=pass\n"
                "MACHINE_DOCTOR: machine=m crit=0 warn=0 unknown=0 ok=true"
            ),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr

    calls = _az_calls(stubs)
    assert any(c.startswith("vm create") for c in calls), "each role must create its own VM"

    ssh_calls = _ssh_calls(stubs)
    provision_calls = [c for c in ssh_calls if "provision-machine.sh --role" in c]
    assert len(provision_calls) == 1
    assert f"--role {role} --machine" in provision_calls[0]

    doctor_calls = [c for c in ssh_calls if "coord machine doctor" in c]
    assert doctor_calls, ssh_calls
    assert f"--role {doctor_role}" in doctor_calls[0]

    if role == "server":
        assert any("7435/board" in c for c in ssh_calls)
    else:
        assert not any("7435/board" in c for c in ssh_calls), "non-server roles must not run coord serve"


def test_two_invocations_without_shared_rg_get_two_different_resource_groups(stubs: dict) -> None:
    r1 = _run(stubs, "--role", "worker")
    r2 = _run(stubs, "--role", "worker")
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert r2.returncode == 0, r2.stdout + r2.stderr

    calls = _az_calls(stubs)
    create_groups = {c.split()[3] for c in calls if c.startswith("group create -n")}
    assert len(create_groups) == 2, f"expected two distinct resource groups, got {create_groups}"


# ===========================================================================
# 2. idempotency: a re-run against an already-provisioned VM is a no-op
# ===========================================================================


def test_rerun_against_an_existing_rg_reuses_the_vm_and_creates_nothing_new(stubs: dict) -> None:
    first = _run(stubs, "--role", "worker", "--rg", "rg-idem", "--keep")
    assert first.returncode == 0, first.stdout + first.stderr
    assert len([c for c in _az_calls(stubs) if c.startswith("vm create")]) == 1

    # --keep must not have torn it down.
    assert not any(c.startswith("group delete") for c in _az_calls(stubs))

    second = _run(
        stubs,
        "--role",
        "worker",
        "--rg",
        "rg-idem",
        env_overrides={
            "STUB_GROUP_EXISTS": "1",
            "STUB_EXISTING_VM_NAME": "throwaway",
            "STUB_PROVISION_OUTPUT": (
                # A real re-run: nothing left to do.
                "PROVISION: role=worker machine=m phases=8 changes=0 dead_exec=0 gate=pass\n"
                "MACHINE_DOCTOR: machine=m crit=0 warn=0 unknown=0 ok=true"
            ),
        },
    )
    assert second.returncode == 0, second.stdout + second.stderr

    calls = _az_calls(stubs)
    # Still exactly one `vm create` across BOTH runs -- the second run must not
    # have made another.
    assert len([c for c in calls if c.startswith("vm create")]) == 1, calls
    assert len([c for c in calls if c.startswith("network nsg rule create")]) == 1, calls
    # This run does tear down (no --keep the second time).
    assert any(c.startswith("group delete") for c in calls)


# ===========================================================================
# 3. evidence capture
# ===========================================================================


def test_evidence_file_contains_both_trailers_and_the_supplementary_probes(stubs: dict) -> None:
    out = stubs["tmp"] / "my-evidence.log"
    env = dict(stubs["env"])
    result = subprocess.run(
        [str(SCRIPT), "--role", "server", "--rg", "rg-ev", "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(stubs["tmp"]),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert out.exists()
    text = out.read_text()
    assert "PROVISION: role=" in text
    assert "MACHINE_DOCTOR: machine=" in text
    assert "coord status" in text
    assert "/health" in text
    assert "/board" in text
    assert "THROWAWAY_VM: role=server" in text
    assert "ok=true" in text
