#!/usr/bin/env bash
# Take a STOCK Ubuntu 24.04 Azure VM through scripts/provision-machine.sh and
# destroy it (#3151).
#
#   ./throwaway-vm.sh --role thin-client|worker|server [options]
#
# WHY THIS EXISTS
# ----------------
# #3138 merged with its own acceptance criterion unmet: run
# provision-machine.sh on a REAL, FRESH Ubuntu 24.04 box and paste the result.
# A worker session cannot do that (no root, no docker/podman/multipass/qemu),
# and scripts/verify-provision-noble.sh -- real, but a chroot in a user
# namespace, not a machine -- says so in its own header. This script is the
# missing mechanical step: it makes the throwaway-VM run a single command an
# operator can run once per role, so the AC stops depending on someone
# remembering to do it by hand.
#
# THE BASE IMAGE IS THE WHOLE POINT.
# -----------------------------------
# This deploys the stock `Canonical:ubuntu-24_04-lts:server:latest` image --
# never a `SOURCE_IMAGE_ID` / the shared gallery image build-worker-image.sh
# publishes. That gallery image is the GOLDEN image, already loaded with every
# prerequisite provision-worker.sh installs; running provision-machine.sh
# against it would prove nothing, because the entire claim under test is the
# fresh-OS path. This script never reads `$SOURCE_IMAGE_ID` or
# `~/.coord/epic.env` -- on purpose, see below.
#
# NO SHARED FLEET INFRASTRUCTURE.
# --------------------------------
# `~/.coord/epic.env` (Key Vault, managed identity, private DNS zone, the
# pre-baked image pin) is the *shared fleet-joining* infrastructure
# `bootstrap-shared.sh` produces for epic-up.sh/epic-down.sh. A disposable
# verification VM needs none of it -- depending on it would couple a
# throwaway check to a one-time setup step having already run on this
# operator's machine, for no benefit.
#
# THE CREDENTIALS PHASE IS INTERACTIVE ON PURPOSE, AND STAYS THAT WAY.
# ----------------------------------------------------------------------
# provision-machine.sh's `credentials` phase runs `sudo tailscale up` (a
# browser URL) and `gh auth login` (a device-flow code) -- there is
# deliberately no auth-key/token env knob, because the design front-loads
# every credential prompt into one phase. This script does not fake that: it
# hands the operator a real interactive `ssh -t` session for the whole
# provisioning run (every other phase is non-interactive, so in practice only
# the credentials phase needs a human -- roughly five minutes of clicking).
# Piping fake input into `tailscale up`/`gh auth login` would produce a green
# result that means nothing; adding non-interactive auth to
# provision-machine.sh itself is the right follow-up for CI and explicitly
# out of scope here.
#
# ONE FRESH VM PER ROLE, PLUS AN EXPLICIT IDEMPOTENCY RE-RUN.
# ---------------------------------------------------------------
# The claim under test is "fresh OS -> fleet member", so reusing one VM
# across `thin-client`/`worker`/`server` would invalidate two of the three
# runs. By default every invocation gets its own resource group (named from
# the role and this process's PID), so three plain invocations produce three
# fresh VMs. Passing the SAME `--rg` (and `--keep` on the first run, so the
# VM survives to be reused) makes the second invocation a genuine no-op
# re-run against an already-provisioned VM -- the idempotency half of the AC
# -- because provision-machine.sh's own phases are all "skip if already done".
#
# TEARDOWN IS A TRAP, ALWAYS.
# ------------------------------
# A leaked VM is a standing bill nobody notices. `cleanup()` below runs from
# a `trap ... EXIT` (which bash also invokes on an otherwise-fatal, untrapped
# signal) plus an explicit `INT`/`TERM` trap that exits through it, so success,
# a `die`, and Ctrl-C all delete the resource group the same way. `--keep`
# is the one deliberate override, and it prints exactly what to run by hand.
#
# FAIL CLOSED (#2096).
# ---------------------
# An absent `PROVISION:` / `MACHINE_DOCTOR:` trailer in the captured output is
# a failure, not a pass -- provision-machine.sh already treats it that way
# (see its phase_gate) and this script does not soften that on the way back
# out. The VM never becoming reachable over SSH is the same kind of failure.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"

log()  { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf '\nthrowaway-vm: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    cat <<'USAGE'
usage: throwaway-vm.sh --role thin-client|worker|server [options]

  --role ROLE            required; thin-client | worker | server
  --rg NAME               resource group name (default: rg-coord-throwaway-<role>-<pid>).
                          If it already exists, its VM is REUSED instead of a
                          new one being created -- this is how you drive the
                          idempotency re-run: pass the same --rg (and --keep
                          the first time) to point a second invocation at the
                          same already-provisioned VM.
  --machine NAME          board machine name passed to provision-machine.sh's
                          --machine (default: throwaway-<role>-<pid>)
  --location LOC          Azure region (default: eastus)
  --vm-size SIZE          VM size (default: Standard_D4as_v7)
  --admin-user USER       VM admin username (default: azureuser)
  --out FILE              where the captured evidence is written (default:
                          ./throwaway-vm-<role>-<timestamp>.log)
  --provision-args ARGS   extra arguments passed through verbatim to
                          provision-machine.sh, e.g. "--capabilities rust,gtk"
  --keep                  do NOT delete the resource group on exit. Use this
                          for the first half of an idempotency check; you are
                          responsible for deleting it afterwards.
  --wait-teardown         after the delete is submitted, poll 'az group show'
                          until the resource group actually 404s (bounded
                          timeout) and report confirmed deletion. Without
                          this, '--no-wait' means the script only knows the
                          delete was SUBMITTED, not that it finished --
                          #3152's repeated runs should pass this.
  -h, --help              this

The credentials phase is interactive by design (tailscale up, gh auth login,
the Claude Code OAuth login) -- you will get a real 'ssh -t' session and need
to be at the keyboard for it. Everything else is unattended.
USAGE
}

main() {
ROLE=""
RG=""
MACHINE_NAME=""
LOCATION="eastus"
VM_SIZE="Standard_D4as_v7"
ADMIN_USER="azureuser"
OUT_FILE=""
PROVISION_ARGS=""
KEEP=0
WAIT_TEARDOWN=0
VM_NAME="throwaway"

# Overridable only so the test suite can drive the reachability loop in
# milliseconds instead of minutes; production defaults match
# build-worker-image.sh's own wait loop (30 x 10s).
SSH_WAIT_ATTEMPTS="${THROWAWAY_VM_SSH_WAIT_ATTEMPTS:-30}"
SSH_WAIT_SLEEP="${THROWAWAY_VM_SSH_WAIT_SLEEP:-10}"

# Same pattern, for --wait-teardown's post-delete confirmation poll.
TEARDOWN_WAIT_ATTEMPTS="${THROWAWAY_VM_TEARDOWN_WAIT_ATTEMPTS:-30}"
TEARDOWN_WAIT_SLEEP="${THROWAWAY_VM_TEARDOWN_WAIT_SLEEP:-10}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --role|--rg|--machine|--location|--vm-size|--admin-user|--out|--provision-args)
            [[ $# -ge 2 ]] || { usage >&2; die "$1 requires a value"; }
            ;;
    esac
    case "$1" in
        --role)            ROLE="$2"; shift 2 ;;
        --rg)              RG="$2"; shift 2 ;;
        --machine)         MACHINE_NAME="$2"; shift 2 ;;
        --location)        LOCATION="$2"; shift 2 ;;
        --vm-size)         VM_SIZE="$2"; shift 2 ;;
        --admin-user)      ADMIN_USER="$2"; shift 2 ;;
        --out)             OUT_FILE="$2"; shift 2 ;;
        --provision-args)  PROVISION_ARGS="$2"; shift 2 ;;
        --keep)            KEEP=1; shift ;;
        --wait-teardown)   WAIT_TEARDOWN=1; shift ;;
        -h|--help)         usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

case "$ROLE" in
    thin-client|worker|server) ;;
    "") usage >&2; die "--role is required (thin-client | worker | server)" ;;
    *)  die "unknown --role '$ROLE' — expected thin-client, worker or server" ;;
esac

# The doctor's own role names (matches provision-machine.sh).
case "$ROLE" in
    server) DOCTOR_ROLE="daemon" ;;
    *)      DOCTOR_ROLE="worker" ;;
esac

[[ -n "$RG" ]]            || RG="rg-coord-throwaway-${ROLE}-$$"
[[ -n "$MACHINE_NAME" ]]  || MACHINE_NAME="throwaway-${ROLE}-$$"
[[ -n "$OUT_FILE" ]]      || OUT_FILE="throwaway-vm-${ROLE}-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"

for tool in az ssh scp curl; do
    have "$tool" || die "required tool '$tool' is not on PATH"
done

: > "$OUT_FILE" || die "cannot write to --out file $OUT_FILE"

log "0/5 preflight"
az account show --query '{sub:name, id:id}' -o tsv
MY_IP="$(curl -fsS https://api.ipify.org)"
[[ -n "$MY_IP" ]] || die "could not determine this operator's egress IP (curl api.ipify.org failed)"
info "SSH will be restricted to $MY_IP/32"
info "resource group: $RG   role: $ROLE   machine: $MACHINE_NAME   evidence: $OUT_FILE"

# ── teardown, on a trap, unconditionally ─────────────────────────────────────

CLEANED_UP=0
cleanup() {
    # #2096: the exit status that was ALREADY PENDING when this trap fired —
    # captured before anything else touches $? — is what a teardown failure
    # gets compared against below, so a failure discovered here can flip a
    # would-be-clean run to non-zero without clobbering an existing failure's
    # own exit code.
    local trigger_status=$?
    [[ $CLEANED_UP -eq 1 ]] && return
    CLEANED_UP=1
    if [[ $KEEP -eq 1 ]]; then
        log "--keep: leaving $RG standing for inspection / an idempotency re-run"
        info "delete it yourself when done:  az group delete -n $RG --yes"
        return
    fi
    log "teardown: deleting resource group $RG"
    local delete_stderr delete_rc=0
    delete_stderr="$(mktemp)"
    # Keep az's own stderr instead of discarding it — it is the one message
    # that would explain a submission failure.
    az group delete -n "$RG" --yes --no-wait -o none 2>"$delete_stderr" || delete_rc=$?
    if [[ $delete_rc -ne 0 ]]; then
        warn "'az group delete -n $RG --yes --no-wait' failed to submit (exit $delete_rc) — $RG may still exist and still be billing."
        if [[ -s "$delete_stderr" ]]; then
            warn "az said:"
            while IFS= read -r line; do warn "  $line"; done < "$delete_stderr"
        fi
        warn "delete it by hand:  az group delete -n $RG --yes"
        rm -f "$delete_stderr"
        # A teardown that failed to even submit must not be reported as a
        # clean run — but don't paper over an existing non-zero exit either.
        [[ $trigger_status -eq 0 ]] && exit 1
        return
    fi
    rm -f "$delete_stderr"
    # --no-wait means "submitted" is the strongest claim available right
    # now — NOT "accepted" or "deleted". The delete could still fail after
    # this (a resource lock, a stuck child resource).
    info "delete submitted (async, --no-wait) — 'az group show -n $RG' will 404 once it finishes"

    [[ $WAIT_TEARDOWN -eq 1 ]] || return
    log "--wait-teardown: polling until $RG actually disappears"
    local attempt=0
    while [[ $attempt -lt $TEARDOWN_WAIT_ATTEMPTS ]]; do
        if ! az group show -n "$RG" -o none >/dev/null 2>&1; then
            info "confirmed: resource group $RG is gone"
            return
        fi
        attempt=$((attempt + 1))
        sleep "$TEARDOWN_WAIT_SLEEP"
    done
    warn "timed out after $TEARDOWN_WAIT_ATTEMPTS attempt(s) waiting for $RG to disappear — it may still exist."
    warn "verify by hand:  az group show -n $RG"
    [[ $trigger_status -eq 0 ]] && exit 1
}
trap cleanup EXIT
# Bash also runs the EXIT trap on an otherwise-fatal untrapped signal, but an
# explicit INT/TERM trap that exits through it makes "Ctrl-C tears down the
# VM" a documented behaviour rather than an implementation detail relied on
# by accident.
trap 'exit 130' INT TERM

# ── create, or reuse, the resource group + VM ────────────────────────────────

REUSED=0
if az group show -n "$RG" -o none >/dev/null 2>&1; then
    REUSED=1
    log "1/5 resource group $RG already exists — reusing its VM (idempotency re-run)"
else
    log "1/5 create resource group + VM ($VM_SIZE, stock Ubuntu 24.04)"
    az group create -n "$RG" -l "$LOCATION" -o none
fi

if [[ $REUSED -eq 0 ]]; then
    # THE stock Canonical SKU. Never $SOURCE_IMAGE_ID / the golden gallery
    # image — see the file header for why that would prove nothing.
    az vm create -g "$RG" -n "$VM_NAME" \
        --image Canonical:ubuntu-24_04-lts:server:latest \
        --size "$VM_SIZE" \
        --admin-username "$ADMIN_USER" --generate-ssh-keys \
        --public-ip-sku Standard --nsg-rule NONE -o none

    # `az vm open-port` cannot restrict a source range (no
    # --source-address-prefixes flag), and an unrestricted :22 on a public IP
    # is not acceptable even for a short-lived verification VM.
    NSG_NAME="$(az vm show -g "$RG" -n "$VM_NAME" \
        --query 'networkProfile.networkInterfaces[0].id' -o tsv 2>/dev/null \
      | xargs -r -I{} az network nic show --ids {} \
        --query 'networkSecurityGroup.id' -o tsv 2>/dev/null)"
    NSG_NAME="${NSG_NAME##*/}"
    [[ -n "$NSG_NAME" ]] || NSG_NAME="${VM_NAME}NSG"   # az vm create's default name
    az network nsg rule create -g "$RG" --nsg-name "$NSG_NAME" -n AllowSshFromOperator \
        --priority 1000 --direction Inbound --access Allow --protocol Tcp \
        --source-address-prefixes "$MY_IP/32" --source-port-ranges '*' \
        --destination-address-prefixes '*' --destination-port-ranges 22 -o none
else
    VM_NAME="$(az vm list -g "$RG" --query '[0].name' -o tsv 2>/dev/null)"
    [[ -n "$VM_NAME" ]] || die "resource group $RG exists but has no VM in it — delete it
by hand (az group delete -n $RG --yes) and re-run without --rg, or with a
--rg that has not been used yet."
fi

IP="$(az vm show -d -g "$RG" -n "$VM_NAME" --query publicIps -o tsv 2>/dev/null)"
[[ -n "$IP" ]] || die "VM $VM_NAME in $RG has no public IP"
info "VM: $VM_NAME at $IP"

log "2/5 wait for SSH"
SSH="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 ${ADMIN_USER}@${IP}"
REACHABLE=0
attempt=0
while [[ $attempt -lt $SSH_WAIT_ATTEMPTS ]]; do
    if $SSH true 2>/dev/null; then REACHABLE=1; break; fi
    attempt=$((attempt + 1))
    sleep "$SSH_WAIT_SLEEP"
done
# #2096: an observation taken AFTER the action, not the mere absence of an
# earlier exception — the loop above is exactly that observation, and its
# result (not "az vm create exited 0") is what gates the rest of the run.
[[ $REACHABLE -eq 1 ]] \
    || die "VM $VM_NAME at $IP never became reachable over SSH after $SSH_WAIT_ATTEMPTS attempt(s).
An unreachable VM cannot be provisioned or verified — this is a failure, not
a partial pass."

log "3/5 stage provision-machine.sh + install-agent.sh"
$SSH "mkdir -p ~/code-coordinator/scripts"
scp -o StrictHostKeyChecking=accept-new \
    "$REPO_ROOT/scripts/provision-machine.sh" "${ADMIN_USER}@${IP}:~/code-coordinator/scripts/"
scp -o StrictHostKeyChecking=accept-new \
    "$REPO_ROOT/install-agent.sh" "${ADMIN_USER}@${IP}:~/code-coordinator/"
$SSH "chmod +x ~/code-coordinator/scripts/provision-machine.sh ~/code-coordinator/install-agent.sh"

log "4/5 provision — INTERACTIVE, the credentials phase needs you at the keyboard"
cat <<EOF
    provision-machine.sh's 'credentials' phase runs 'sudo tailscale up' (a
    browser URL) and 'gh auth login' (a device-flow code), among others.
    Nothing here fakes those prompts. You are about to get a real interactive
    SSH session; everything else in this run is unattended, so once the
    credentials phase is done you can walk away.
EOF
REMOTE_CMD="cd ~/code-coordinator && ./scripts/provision-machine.sh --role $ROLE --machine $MACHINE_NAME $PROVISION_ARGS"
set +e
ssh -t -o StrictHostKeyChecking=accept-new "${ADMIN_USER}@${IP}" "$REMOTE_CMD" 2>&1 | tee -a "$OUT_FILE"
STATUS="${PIPESTATUS[0]}"
set -e

# The verdict is read from THIS transcript — the provisioning run itself —
# BEFORE the supplementary evidence commands below get a chance to append
# their own (informational, best-effort) MACHINE_DOCTOR output to the same
# file. Deciding pass/fail from the file's full, final contents would let a
# healthy re-run of 'coord machine doctor' during evidence capture paper over
# a provisioning run that never produced a trailer at all — a gate that
# cannot fail is not a gate (#2096).
PROVISION_TRAILER="$(grep -E '^PROVISION: ' "$OUT_FILE" | tail -1 || true)"
DOCTOR_TRAILER="$(grep -E '^MACHINE_DOCTOR: ' "$OUT_FILE" | tail -1 || true)"

log "5/5 capture evidence"
{
    printf '\n--- coord machine doctor (re-run, non-interactive) ---\n'
    $SSH "PATH=\"\$HOME/.coord-venv/bin:\$PATH\"; coord machine doctor $MACHINE_NAME --ssh -v --role $DOCTOR_ROLE" 2>&1 || true
    printf '\n--- coord status ---\n'
    $SSH "PATH=\"\$HOME/.coord-venv/bin:\$PATH\"; coord status" 2>&1 || true
    printf '\n--- /health ---\n'
    $SSH "curl -fsS http://127.0.0.1:7433/health" 2>&1 || true
    printf '\n--- /board ---\n'
    if [[ "$ROLE" == "server" ]]; then
        $SSH "curl -fsS http://127.0.0.1:7435/board" 2>&1 || true
    else
        printf '(skipped — role=%s does not run coord serve)\n' "$ROLE"
    fi
} >> "$OUT_FILE"
tail -n 40 "$OUT_FILE"

# ── fail closed: the trailers from the provisioning run, not the exit code
#    and not anything the evidence step above appended, are the verdict
#    (#2096) ─────────────────────────────────────────────────────────────────

OK=true
REASON=""
if [[ -z "$DOCTOR_TRAILER" ]]; then
    OK=false; REASON="no MACHINE_DOCTOR: trailer in the captured output — an absent verdict is a failure, not a pass"
elif [[ "$DOCTOR_TRAILER" != *"ok=true"* ]]; then
    OK=false; REASON="MACHINE_DOCTOR trailer reports a failing doctor: $DOCTOR_TRAILER"
elif [[ -z "$PROVISION_TRAILER" ]]; then
    OK=false; REASON="no PROVISION: trailer in the captured output — provision-machine.sh did not reach its own end"
elif [[ "$PROVISION_TRAILER" != *"gate=pass"* ]]; then
    OK=false; REASON="PROVISION trailer did not report gate=pass: $PROVISION_TRAILER"
elif [[ "$STATUS" -ne 0 ]]; then
    OK=false; REASON="provision-machine.sh's ssh session exited $STATUS despite trailers looking clean"
fi

printf 'THROWAWAY_VM: role=%s rg=%s machine=%s vm=%s ok=%s\n' \
    "$ROLE" "$RG" "$MACHINE_NAME" "$VM_NAME" "$OK" | tee -a "$OUT_FILE"

if [[ "$OK" != "true" ]]; then
    die "$REASON
Evidence (including whatever WAS captured) is in $OUT_FILE."
fi

info "evidence captured to $OUT_FILE"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
