#!/usr/bin/env bash
# Check everything the epic-VM stack needs, in one shot, before you run
# anything that creates resources. Read-only — creates nothing, changes nothing.
#
#   ./preflight.sh --vault kv-coord-jd-prod [--location eastus] [--vm-size Standard_D8as_v7]
#   ./preflight.sh --role server --vault kv-coord-jd-prod     # the #3130 DR board
#
# Exit 0 = clear to run bootstrap-shared.sh. Exit 1 = at least one FAIL.
#
# --role server (#3130, rung D4) checks the *other* role in this lane: a board
# daemon standing in for a lost site, rather than a worker. It runs dr-up.sh's
# OWN gate functions rather than a second copy of them, so a check that passes
# here and a check that passes in `dr-up.sh --dry-run` cannot drift apart
# (#2085). It also drops section H: in a site-loss scenario the daemon host is
# precisely the thing that is gone, so `ssh dellserver` failing is the premise,
# not a blocker.
set -uo pipefail   # deliberately NOT -e: we want every check to run

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VAULT=""; LOCATION="eastus"; VM_SIZE=""; ROLE="worker"
DAEMON_HOST="${DAEMON_HOST:-dellserver}"
DR_MACHINE="coord-dr"
while [[ $# -gt 0 ]]; do
    case $1 in
        --vault)    VAULT="$2"; shift 2 ;;
        --location) LOCATION="$2"; shift 2 ;;
        --vm-size)  VM_SIZE="$2"; shift 2 ;;
        --daemon)   DAEMON_HOST="$2"; shift 2 ;;
        --role)     ROLE="$2"; shift 2 ;;
        --machine)  DR_MACHINE="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
case "$ROLE" in
    worker|server) ;;
    *) echo "unknown --role '$ROLE' (expected: worker, server)" >&2; exit 2 ;;
esac
# Role-appropriate default SKU. A board daemon serves HTTP and runs sqlite; it
# is not a build box.
if [[ -z "$VM_SIZE" ]]; then
    if [[ "$ROLE" == "server" ]]; then VM_SIZE="Standard_D4as_v7"; else VM_SIZE="Standard_D8as_v7"; fi
fi

PASS=0; WARN=0; FAIL=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; WARN=$((WARN+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ==========================================================================
hdr "A. Local tooling"
for t in az jq curl ssh scp python3 tailscale; do
    if command -v "$t" >/dev/null 2>&1; then
        ok "$t present"
    else
        case $t in
            az)  bad "az missing — curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash" ;;
            jq)  bad "jq missing — sudo apt install -y jq  (epic-up/down parse /health with it)" ;;
            tailscale) bad "tailscale missing — the scripts reach the worker over the tailnet" ;;
            *)   bad "$t missing" ;;
        esac
    fi
done
if command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,12) else 1)'; then
    ok "python3 $(python3 -V 2>&1 | cut -d' ' -f2) (>=3.12)"
else
    warn "python3 <3.12 locally — only matters on the worker image, not here"
fi

# Azure checks need az+jq, but the tailnet and daemon-host checks do not — and
# the whole point of this script is to surface every problem in one run rather
# than one per re-run. So skip the Azure sections, don't abort.
SKIP_AZURE=0
if ! command -v az >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    SKIP_AZURE=1
fi

# ==========================================================================
hdr "B. Azure authentication"
if (( SKIP_AZURE )); then
    warn "skipped — needs az and jq (sections B-E)"
elif ACCOUNT="$(az account show -o json 2>/dev/null)"; then
    SUB_ID="$(jq -r .id <<<"$ACCOUNT")"
    ok "logged in: $(jq -r .name <<<"$ACCOUNT") ($(jq -r .user.name <<<"$ACCOUNT"))"
else
    bad "not logged in — run: az login"
    SKIP_AZURE=1
fi

ME=""
(( SKIP_AZURE )) || ME="$(az ad signed-in-user show --query id -o tsv 2>/dev/null)"
if (( SKIP_AZURE )); then :
elif [[ -n "$ME" ]]; then
    # bootstrap-shared.sh creates TWO role assignments. Contributor cannot do
    # that — it needs Owner, User Access Administrator, or RBAC Administrator.
    ROLES="$(az role assignment list --assignee "$ME" --include-inherited --include-groups \
             --scope "/subscriptions/${SUB_ID}" --query '[].roleDefinitionName' -o tsv 2>/dev/null)"
    if grep -qiE 'Owner|User Access Administrator|Role Based Access Control Administrator' <<<"$ROLES"; then
        ok "can create role assignments ($(tr '\n' ',' <<<"$ROLES" | sed 's/,$//'))"
    else
        bad "no role-assignment rights. Have: ${ROLES//$'\n'/, }. Need Owner or User Access Administrator."
    fi
else
    warn "could not resolve signed-in user (service principal?) — verify role-assignment rights by hand"
fi

# ==========================================================================
hdr "C. Resource providers"
if (( SKIP_AZURE )); then warn "skipped — needs an authenticated az session"; else
# A subscription that has never used a service leaves its provider unregistered,
# and deployment fails with MissingSubscriptionRegistration rather than anything
# obvious. Registration is free and idempotent.
for p in Microsoft.Compute Microsoft.Network Microsoft.KeyVault Microsoft.ManagedIdentity Microsoft.Authorization; do
    state="$(az provider show -n "$p" --query registrationState -o tsv 2>/dev/null)"
    case "$state" in
        Registered)  ok "$p registered" ;;
        Registering) warn "$p still registering — wait, then re-check" ;;
        *)           bad "$p is '$state' — run: az provider register -n $p" ;;
    esac
done

fi

# ==========================================================================
hdr "D. Compute quota in $LOCATION"
if [[ "$ROLE" == "server" ]]; then
    # Section I runs dr-up.sh's own check_quota for the SERVER SKU. Running
    # this worker-sized (8 vCPU) check too would report a second, different
    # quota verdict for the same subscription — two answers to one question.
    warn "skipped — --role server: section I checks quota for the server SKU ($VM_SIZE)"
elif (( SKIP_AZURE )); then warn "skipped — needs an authenticated az session"; else
# TWO limits apply and both bite. The per-family limit gates the SKU; the
# regional "cores" total gates everything at once, so it is what decides
# whether you can run more than one worker (or a worker while the image
# builder runs). A fresh subscription commonly has cores=14, which is a
# single 8-vCPU VM and nothing else.
NEEDED=8
# NB: `az vm list-skus -n <sku>` is NOT valid (no -n flag) — an earlier version
# of this check used it, silently got an empty family, and degraded to a WARN.
# That let a hard QuotaExceeded failure through to deployment. Query by name.
# `az vm list-skus` enumerates every SKU in the region and routinely takes
# minutes, so bound it and fall back rather than hanging the whole preflight.
FAMILY="$(timeout 45 az vm list-skus -l "$LOCATION" --resource-type virtualMachines \
          --query "[?name=='$VM_SIZE'].family | [0]" -o tsv 2>/dev/null)"
if [[ -z "$FAMILY" ]]; then
    # Derive it from the SKU name: Standard_D8as_v7 -> StandardDasv7Family.
    # Only used to look up a quota row; a miss degrades to an explicit FAIL below.
    FAMILY="$(python3 - "$VM_SIZE" <<'PYEOF'
import re, sys
m = re.match(r'Standard_([A-Z]+)(\d+)(-\d+)?([a-z]*)_?v?(\d+)?', sys.argv[1])
if m:
    series, _, _, suffix, ver = m.groups()
    print(f"Standard{series.capitalize()}{suffix}v{ver}Family" if ver else "")
PYEOF
)"
    [[ -n "$FAMILY" ]] && printf '        (SKU lookup timed out; inferred family %s from the name)\n' "$FAMILY"
fi
USAGE="$(az vm list-usage -l "$LOCATION" -o json 2>/dev/null)"

quota_of() {  # quota_of <family-name> -> "current limit", empty if unknown
    python3 -c "
import json,sys
u=json.load(sys.stdin)
for e in u:
    if e['name']['value'].lower()==sys.argv[1].lower():
        print(e['currentValue'], e['limit']); break" "$1" <<<"$USAGE" 2>/dev/null
}

if [[ -z "$FAMILY" ]]; then
    bad "could not resolve the VM family for $VM_SIZE in $LOCATION — cannot verify quota"
else
    read -r cur lim <<<"$(quota_of "$FAMILY")"
    if [[ -z "${lim:-}" ]]; then
        bad "$FAMILY quota not reported — cannot verify, treat as unknown"
    elif (( lim - cur >= NEEDED )); then
        ok "$FAMILY: $((lim-cur))/$lim vCPUs free (need $NEEDED)"
    else
        bad "$FAMILY: $((lim-cur))/$lim vCPUs free, need $NEEDED — request a quota increase"
    fi
fi

read -r cur lim <<<"$(quota_of cores)"
if [[ -z "${lim:-}" ]]; then
    bad "regional 'cores' quota not reported — cannot verify"
else
    avail=$(( lim - cur ))
    if (( avail >= NEEDED )); then
        ok "regional cores: $avail/$lim free (need $NEEDED)"
        (( avail < NEEDED * 2 )) && warn "only $avail regional cores — enough for ONE ${NEEDED}-vCPU VM, so no parallel epics and no worker while the image builds"
    else
        bad "regional cores: $avail/$lim free, need $NEEDED — request a regional vCPU increase"
    fi
fi

fi

hdr "E. Key Vault name"
if (( SKIP_AZURE )); then warn "skipped — needs an authenticated az session"; else
if [[ -z "$VAULT" ]]; then
    warn "no --vault given — skipping name availability check"
else
    # Vault names are GLOBALLY unique across all of Azure.
    avail="$(az keyvault check-name --name "$VAULT" -o json 2>/dev/null)"
    if [[ "$(jq -r .nameAvailable <<<"$avail" 2>/dev/null)" == "true" ]]; then
        ok "'$VAULT' is available"
    else
        reason="$(jq -r '.reason // "unknown"' <<<"$avail" 2>/dev/null)"
        bad "'$VAULT' unavailable ($reason) — vault names are globally unique, pick another"
    fi
    # A soft-deleted vault holds its name for the retention period and blocks reuse.
    if az keyvault list-deleted --query "[?name=='$VAULT']" -o tsv 2>/dev/null | grep -q .; then
        bad "'$VAULT' exists as SOFT-DELETED — purge it or choose another name"
    fi
fi

fi

# ==========================================================================
hdr "F. SSH key"
KEYFILE="${SSH_PUBLIC_KEY_FILE:-$HOME/.ssh/id_ed25519.pub}"
[[ -f "$KEYFILE" ]] && ok "$KEYFILE present" \
    || bad "$KEYFILE missing — ssh-keygen -t ed25519, or set SSH_PUBLIC_KEY_FILE in epic.env"

# ==========================================================================
hdr "G. Tailnet"
if command -v tailscale >/dev/null 2>&1; then
    if tailscale status >/dev/null 2>&1; then
        ok "tailscale up ($(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("MagicDNSSuffix","?"))' 2>/dev/null || echo "?"))"
        if tailscale status 2>/dev/null | grep -q "$DAEMON_HOST"; then
            ok "$DAEMON_HOST visible on the tailnet"
        elif [[ "$ROLE" == "server" ]]; then
            # The premise of --role server is that the daemon host is gone.
            # Failing on its absence would make the DR preflight unusable in
            # exactly the scenario it exists for.
            warn "$DAEMON_HOST not on the tailnet — expected for a site loss; that is why you are running --role server"
        else
            bad "$DAEMON_HOST not on the tailnet — epic-up registers the machine there"
        fi
    else
        bad "tailscale is installed but not connected — run: tailscale up"
    fi
fi
# The ACL cannot be inspected without a Tailscale API key, so this is on you.
warn "confirm by hand: tag:coord-worker exists in the tailnet ACL, and an OAuth"
warn "  client with auth_keys scope is tagged with it (bootstrap prompts for its secret)"
if [[ "$ROLE" == "server" ]]; then
    warn "  ...and tag:coord-server too (section I checks the policy FILE; the live"
    warn "  tailnet needs \$TAILSCALE_API_KEY to be checkable at all)"
fi

# ==========================================================================
hdr "H. Daemon host ($DAEMON_HOST)"
if [[ "$ROLE" == "server" ]]; then
    warn "skipped — --role server presumes $DAEMON_HOST is gone. epic-up/down edit its"
    warn "  coordinator.yml over ssh; dr-up.sh does not, because there is nothing to edit."
elif ssh -o BatchMode=yes -o ConnectTimeout=8 "$DAEMON_HOST" true 2>/dev/null; then
    ok "ssh $DAEMON_HOST works without a password"

    REMOTE_COORD='c=""; for p in "$HOME/.coord-venv/bin/coord" "$HOME/.local/bin/coord" "$(command -v coord 2>/dev/null)"; do [ -n "$p" ] && [ -x "$p" ] && { c="$p"; break; }; done; echo "$c"'
    COORD_PATH="$(ssh "$DAEMON_HOST" "$REMOTE_COORD" 2>/dev/null)"
    if [[ -n "$COORD_PATH" ]]; then
        ok "coord at $COORD_PATH ($(ssh "$DAEMON_HOST" "'$COORD_PATH' --version" 2>/dev/null | tail -1))"
    else
        bad "no coord binary found on $DAEMON_HOST (looked in ~/.coord-venv/bin, ~/.local/bin, PATH)"
    fi

    if ssh "$DAEMON_HOST" 'test -f ~/.coord/coordinator.yml' 2>/dev/null; then
        ok "~/.coord/coordinator.yml exists (the real config, not a cache)"
        if ssh "$DAEMON_HOST" 'grep -q "epic-machines" ~/.coord/coordinator.yml' 2>/dev/null; then
            ok "epic-machines markers present"
        else
            bad "epic-machines markers missing — add these two lines to the END of the machines: list:
              # >>> epic-machines (managed by epic-up.sh) >>>
              # <<< epic-machines <<<"
        fi
        # Confirm coord.config.load works there — that is exactly how epic-up validates.
        VALIDATOR='P="$(dirname '"'"'@C@'"'"')/python"; [ -x "$P" ] || P="$(head -1 '"'"'@C@'"'"' | sed "s|^#!||")"; "$P" -c "
from pathlib import Path; from coord.config import load
import os; print(len(load(Path(os.path.expanduser(chr(126)+chr(47)+chr(46)+chr(99)+chr(111)+chr(111)+chr(114)+chr(100)+chr(47)+chr(99)+chr(111)+chr(111)+chr(114)+chr(100)+chr(105)+chr(110)+chr(97)+chr(116)+chr(111)+chr(114)+chr(46)+chr(121)+chr(109)+chr(108)))).machines))"'
    VALIDATOR="${VALIDATOR//@C@/$COORD_PATH}"
    if n="$(ssh "$DAEMON_HOST" "$VALIDATOR" 2>/dev/null)" && [[ -n "$n" ]]; then
        ok "coord.config.load() works on the daemon host ($n machines) — the validator epic-up uses"
    else
        bad "coord.config.load() failed on $DAEMON_HOST — epic-up cannot validate config edits"
    fi
    else
        bad "~/.coord/coordinator.yml not found on $DAEMON_HOST"
    fi
else
    bad "cannot ssh to $DAEMON_HOST — epic-up/down edit coordinator.yml over ssh"
fi

# ==========================================================================
# I. DR board server (#3130) — only for --role server.
#
# Every check here is dr-up.sh's OWN gate function, called directly. That is
# deliberate: a preflight that says "clear" and a `dr-up.sh --dry-run` that then
# refuses would be two answers to one question (#2085). Sourcing dr-up.sh is
# side-effect-free — its main() is behind a BASH_SOURCE guard — but it does set
# -e, which this script deliberately does not want, so it is turned back off.
if [[ "$ROLE" == "server" ]]; then
hdr "I. DR board server role"
if [[ ! -f "$HERE/dr-up.sh" ]]; then
    bad "dr-up.sh not found next to preflight.sh ($HERE) — cannot check the server role"
else
    # shellcheck source=./dr-up.sh
    source "$HERE/dr-up.sh"
    set +e   # dr-up.sh sets -euo pipefail; preflight runs every check regardless

    server_gate() {  # server_gate <label> <fn> [args...]
        local label="$1"; shift
        local out
        if out="$("$@" 2>&1)"; then
            ok "$label"
        else
            bad "$label"
        fi
        [[ -n "$out" ]] && sed 's/^/        /' <<<"$out"
        return 0
    }

    server_gate "easy-azure server module" check_server_module "${EASY_AZURE_DIR:-}"
    server_gate "tailnet policy carries tag:coord-server" check_acl_tag "$HERE/tailnet-acl.hujson"
    if (( SKIP_AZURE )); then
        warn "Key Vault secret set + server quota skipped — needs an authenticated az session"
    else
        server_gate "Key Vault holds the server credential set" check_vault_secrets "$VAULT"
        server_gate "vCPU quota for $VM_SIZE in $LOCATION" check_quota "$LOCATION" "$VM_SIZE" "$DR_VCPUS_NEEDED"
    fi
    server_gate "tailnet hostname '$DR_MACHINE' is free" check_hostname_collision "$DR_MACHINE"
fi
fi

# ==========================================================================
printf '\n\033[1m%s\033[0m\n' "Summary"
printf '  %d passed, %d warnings, %d failed\n' "$PASS" "$WARN" "$FAIL"
if (( FAIL > 0 )); then
    printf '\n\033[31mNot ready.\033[0m Fix the FAILs above, then re-run.\n'
    exit 1
fi
if [[ "$ROLE" == "server" ]]; then
    printf '\n\033[32mClear to run:\033[0m ./dr-up.sh --dry-run --vault %s\n' "${VAULT:-<name>}"
    printf 'The dry-run re-runs these same gates and prints the full plan without creating anything.\n'
else
    printf '\n\033[32mClear to run:\033[0m ./bootstrap-shared.sh --rg rg-coord-shared --vault %s\n' "${VAULT:-<name>}"
fi
printf 'Review the WARNs first — the tailnet ACL one is not machine-checkable.\n'
