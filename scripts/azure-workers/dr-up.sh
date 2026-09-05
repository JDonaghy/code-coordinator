#!/usr/bin/env bash
# Stand up a coord BOARD DAEMON in Azure after a site loss (#3130, rung D4 of
# epic #3117 — the Domain-B recovery path).
#
#   ./dr-up.sh --dry-run                       # rehearse: plan only, creates nothing
#   ./dr-up.sh --vault kv-coord-jd-prod        # the real thing
#
# Domain A (rung D3, `coord dr promote` onto precision) covers a dead machine.
# It does nothing for fire, theft, flood or an extended power/ISP outage,
# because precision is in the same room as dellserver. The off-site backup from
# D1 is worthless in that scenario without somewhere to restore it *to*; this
# is that somewhere.
#
# ---------------------------------------------------------------------------
# This is the SERVER role in the existing worker lane, not a second lane
# ---------------------------------------------------------------------------
# `bootstrap-shared.sh`, the Key Vault, the managed identity, the private DNS
# zone, the Tailscale OAuth client and `preflight.sh` are all shared with
# `epic-up.sh`. What is new is a server provisioning path, a distinct tailnet
# tag, and the credential set a worker image deliberately does not carry.
#
# A worker is defined by *not* being a server: no `coord serve`, no store, no
# board token, and an ACL granting `tag:coord-worker` exactly one destination.
# So the inverse rule is the interesting part -- see tailnet-acl.hujson:
#
#   tag:coord-server  <- autogroup:member  : 22, 7433, 7434, 7435
#   tag:coord-server  <- tag:coord-worker  : 7435
#
# ---------------------------------------------------------------------------
# --dry-run is the primary interface
# ---------------------------------------------------------------------------
# A real run creates billable Azure resources and cannot run in CI, so
# `--dry-run` resolves every parameter, runs every gate, and prints the exact
# ordered plan -- resource group, SKU, image, tag, ACL requirements, secrets it
# will fetch -- while issuing no `az` create-family call at all. That is the
# mode the tests drive and the mode an operator rehearses with. A real
# provisioning run is verified in D6's drill, not here.
#
# It exits NON-ZERO when a gate blocks, so a rehearsal that would have produced
# a broken server says so rather than printing a plan nobody re-reads.
set -euo pipefail

DR_UP_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The canonical secret table + the readiness gate live in the boot-time half.
# Sourcing it is side-effect-free (its main() is behind a BASH_SOURCE guard),
# which is what lets `dr-up.sh`, `provision-server.sh` and `preflight.sh` all
# answer "which secrets does a server need?" from ONE table (#2085).
if [[ -z "${DR_PROVISION_SERVER_SOURCED:-}" ]]; then
    # shellcheck source=./provision-server.sh
    source "$DR_UP_HERE/provision-server.sh"
    DR_PROVISION_SERVER_SOURCED=1
fi

# Defaults. A board daemon is not a build box: it serves HTTP, runs sqlite and
# shells out to `gh`. 4 vCPU in the same family the worker lane already holds
# quota in (StandardDasv7Family) rather than a second family to get approved.
DR_VM_SIZE_DEFAULT="Standard_D4as_v7"
DR_VCPUS_NEEDED=4
DR_MACHINE_DEFAULT="coord-dr"
DR_LOCATION_DEFAULT="eastus"

# The easy-azure module a SERVER needs. `modules/coord-worker-vm` exists and
# builds a worker; the server counterpart is the cross-repo half of #3130 and
# is deliberately NOT attempted from this repo -- that is exactly the mistake
# #1777 made (opencode-api-key sat half-done for months because the two halves
# were never scoped separately). This script fails clearly when it is absent
# rather than half-booting a server with no secrets.
DR_SERVER_MODULE_REL="modules/coord-server-vm/main.bicep"

log() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

# --------------------------------------------------------------------------
# Gates. Each prints a human reason on stdout and returns non-zero when it
# blocks, so main() can collect them all in one pass instead of making an
# operator re-run once per problem.
#
# Every one of them is reachable in both directions: the tests drive the
# failing verdict of each, because a gate whose FAIL branch is unreachable is
# not a gate (#2096).
# --------------------------------------------------------------------------

# check_server_module <easy-azure-dir>
check_server_module() {
    local dir="${1:-}"
    local template="${dir%/}/$DR_SERVER_MODULE_REL"
    if [[ -z "$dir" ]]; then
        echo "EASY_AZURE_DIR is unset -- cannot locate $DR_SERVER_MODULE_REL"
        return 1
    fi
    if [[ ! -f "$template" ]]; then
        echo "easy-azure server module absent: $template"
        echo "  The coord-server-vm Bicep module (coord-secrets fetching the SERVER"
        echo "  secret set, coord serve/coord web units, tag:coord-server on the"
        echo "  tailnet join) is the easy-azure half of #3130 and is deliberately not"
        echo "  attempted from this repo. Land it there first -- see #1777 for what"
        echo "  happens when the two halves are not scoped separately."
        return 1
    fi
    echo "easy-azure server module present: $template"
    return 0
}

# check_env_vars
#
# Every ID the deployment needs, resolved from the shared env file. A
# placeholder left unfilled would otherwise surface as an opaque Azure error
# several minutes and one running VM later -- the same guard epic-up.sh and
# epic-down.sh open with.
check_env_vars() {
    local v missing="" placeholder=""
    for v in SUBSCRIPTION_ID KEY_VAULT_URI KEY_VAULT_RESOURCE_ID IDENTITY_RESOURCE_ID \
             IDENTITY_CLIENT_ID PRIVATE_DNS_ZONE_ID SOURCE_IMAGE_ID; do
        if [[ -z "${!v:-}" ]]; then
            missing="${missing:+$missing, }$v"
        elif [[ "${!v}" == *"<"* ]]; then
            placeholder="${placeholder:+$placeholder, }$v"
        fi
    done
    if [[ -n "$missing" || -n "$placeholder" ]]; then
        [[ -n "$missing" ]] && echo "unset in the env file: $missing"
        [[ -n "$placeholder" ]] && echo "still a placeholder in the env file: $placeholder"
        echo "  Populate them from bootstrap-shared.sh's output."
        return 1
    fi
    echo "every shared resource ID is populated"
    return 0
}

# check_acl_tag <acl-file>
#
# "Does tag:coord-server exist in the tailnet policy?"
#
# The authoritative answer lives in the live tailnet, which needs an API key
# this lane does not hold (see preflight.sh section G: the ACL is not
# machine-checkable without one). So the verdict this gate reports is
# explicitly about the POLICY FILE -- which is the artifact an operator
# applies, and which failing is a guaranteed broken run. When
# $TAILSCALE_API_KEY is present the live policy is checked too, and its answer
# wins; when it is not, that is reported as *unverified*, never folded into a
# pass.
check_acl_tag() {
    local acl="${1:-$DR_UP_HERE/tailnet-acl.hujson}"
    if [[ ! -f "$acl" ]]; then
        echo "tailnet policy file not found: $acl"
        return 1
    fi
    if ! grep -q "\"$DR_SERVER_TAG\"" "$acl"; then
        echo "$DR_SERVER_TAG is not declared in $acl"
        echo "  A VM tagged $DR_SERVER_TAG on a tailnet whose policy has no such tag"
        echo "  joins with no grants at all: nothing can reach its board."
        return 1
    fi
    # A plain substring match on "$DR_SERVER_TAG:7435" only recognises the
    # single-port destination form ("tag:coord-server:7435"). This file's own
    # convention keeps worker-facing grants single-port (see tests: below), but
    # an equally valid multi-port destination ("tag:coord-server:7433,7435")
    # would silently fail this check even though it grants exactly what is
    # asked. Match 7435 anywhere in a comma-separated port list instead of only
    # as the sole port -- still not real JSON parsing (this whole gate is
    # explicitly about the POLICY FILE, see the function comment above), just a
    # less brittle grep.
    if ! grep -qE "\"${DR_SERVER_TAG}:([0-9]+,)*7435(,[0-9]+)*\"" "$acl"; then
        echo "$DR_SERVER_TAG is declared in $acl but nothing grants :7435 to it"
        echo "  A board no worker can reach is not a recovered fleet."
        return 1
    fi
    if [[ -n "${TAILSCALE_API_KEY:-}" ]]; then
        local live
        if live="$(curl -fsS --max-time 10 \
                    -u "${TAILSCALE_API_KEY}:" \
                    "https://api.tailscale.com/api/v2/tailnet/-/acl" 2>/dev/null)"; then
            if grep -q "$DR_SERVER_TAG" <<<"$live"; then
                echo "$DR_SERVER_TAG present in the LIVE tailnet policy (and in $acl)"
                return 0
            fi
            echo "$DR_SERVER_TAG is in $acl but NOT in the live tailnet policy -- re-apply the ACL"
            return 1
        fi
        echo "$DR_SERVER_TAG declared in $acl; live policy fetch FAILED (\$TAILSCALE_API_KEY set but the API call did not answer)"
        return 1
    fi
    echo "$DR_SERVER_TAG declared in $acl (live tailnet policy UNVERIFIED -- no \$TAILSCALE_API_KEY; confirm it is applied)"
    return 0
}

# check_vault_secrets <vault>
#
# Names only. `az keyvault secret list --query '[].name'` never returns a
# value, so no secret can reach this process, an argv, or a log line.
check_vault_secrets() {
    local vault="${1:-}"
    if [[ -z "$vault" ]]; then
        echo "no Key Vault name (set KEY_VAULT_NAME in the env file, or pass --vault)"
        return 1
    fi
    local present
    if ! present="$(az keyvault secret list --vault-name "$vault" \
                    --query "[].name" -o tsv 2>/dev/null)"; then
        echo "could not list secrets in Key Vault '$vault' -- no read access, or the vault firewall blocks this IP"
        return 1
    fi
    local name missing=""
    while read -r name; do
        [[ -n "$name" ]] || continue
        grep -qx "$name" <<<"$present" || missing="${missing:+$missing, }$name"
    done < <(dr_secret_names required)
    if [[ -n "$missing" ]]; then
        echo "Key Vault '$vault' is missing required secret(s): $missing"
        echo "  A server needs the credential set a worker is denied. Add them with"
        echo "  bootstrap-shared.sh (prompted, never argv) before provisioning."
        return 1
    fi
    echo "Key Vault '$vault' holds every required secret"
    return 0
}

# dr_family_for_sku <sku> -> the vCPU quota family name.
#
# Pure string derivation, identical to preflight.sh's fallback:
#   Standard_D4as_v7 -> StandardDasv7Family
# `az vm list-skus` is authoritative but routinely takes minutes, and using
# `-n <sku>` on it is not valid (an earlier preflight bug that silently
# degraded a hard QuotaExceeded into a WARN). This is a lookup key only: a
# miss surfaces as an explicit blocker below, never as a pass.
dr_family_for_sku() {
    local sku="$1" body ver series suffix
    [[ "$sku" == Standard_* ]] || return 1
    body="${sku#Standard_}"            # D4as_v7
    if [[ "$body" == *_v* ]]; then
        ver="${body##*_v}"             # 7
        body="${body%%_v*}"            # D4as
    else
        ver=""
    fi
    body="$(sed 's/-[0-9][0-9]*//' <<<"$body")"   # constrained-vCPU: D8-2as -> D8as
    series="${body%%[0-9]*}"           # D
    suffix="${body##*[0-9]}"           # as
    [[ -n "$series" && -n "$ver" ]] || return 1
    echo "Standard${series^}${suffix}v${ver}Family"
}

# dr_quota_free <usage-json> <family> -> "<free> <limit>", non-zero when the
# family has no row at all (which is NOT the same as zero free).
dr_quota_free() {
    local usage="$1" family="$2" row
    row="$(jq -r --arg f "$family" \
        '.[] | select((.name.value // "") | ascii_downcase == ($f | ascii_downcase))
             | "\(.limit - .currentValue) \(.limit)"' <<<"$usage" 2>/dev/null)" || return 1
    [[ -n "$row" ]] || return 1
    echo "$row"
}

# check_quota <location> <vm-size> <needed-vcpus>
#
# TWO limits apply and both bite: the per-family limit gates the SKU, and the
# regional `cores` total gates everything at once. An unreadable quota is a
# BLOCKER, not a pass -- "we could not check" is the permissive default #2096
# rejects, and QuotaExceeded otherwise arrives minutes into a real deployment.
check_quota() {
    local location="$1" vm_size="$2" needed="$3"
    local family usage free limit
    if ! family="$(dr_family_for_sku "$vm_size")"; then
        echo "could not derive a quota family for '$vm_size' -- cannot verify quota"
        return 1
    fi
    if ! usage="$(az vm list-usage -l "$location" -o json 2>/dev/null)" || [[ -z "$usage" ]]; then
        echo "could not read vCPU usage in $location -- cannot verify quota"
        return 1
    fi
    local blocked=0
    if read -r free limit <<<"$(dr_quota_free "$usage" "$family")" && [[ -n "${limit:-}" ]]; then
        if (( free >= needed )); then
            echo "$family: $free/$limit vCPUs free (need $needed)"
        else
            echo "$family: $free/$limit vCPUs free, need $needed -- request a quota increase"
            blocked=1
        fi
    else
        echo "$family quota not reported in $location -- cannot verify, treating as insufficient"
        blocked=1
    fi
    if read -r free limit <<<"$(dr_quota_free "$usage" cores)" && [[ -n "${limit:-}" ]]; then
        if (( free >= needed )); then
            echo "regional cores: $free/$limit free (need $needed)"
        else
            echo "regional cores: $free/$limit free, need $needed -- request a regional vCPU increase"
            blocked=1
        fi
    else
        echo "regional 'cores' quota not reported in $location -- cannot verify, treating as insufficient"
        blocked=1
    fi
    return $(( blocked ))
}

# check_hostname_collision <machine>
#
# A node already holding this hostname makes Tailscale name the new VM
# "<name>-1", and the readiness poll then waits the full timeout on a name that
# resolves to the STALE node. That cost 15 minutes in the worker lane
# (docs/EPHEMERAL_WORKERS.md, "Gotchas"); epic-up.sh already fails fast on it
# and this matches that behaviour rather than re-learning it. It is a separate
# implementation only because epic-up.sh's copy is inline in its main() and
# changing the worker role is out of scope for #3130.
check_hostname_collision() {
    local machine="$1"
    if ! command -v tailscale >/dev/null 2>&1 || ! tailscale status >/dev/null 2>&1; then
        echo "tailscale unavailable locally -- hostname collision UNCHECKED for '$machine'"
        return 0
    fi
    if tailscale status 2>/dev/null | awk '{print $2}' | grep -qx "$machine"; then
        echo "a tailnet node named '$machine' already exists"
        echo "  Tailscale would name the new VM '${machine}-1' and this script would poll"
        echo "  '$machine' -- the stale node -- until it times out."
        echo "  Remove it at https://login.tailscale.com/admin/machines, then re-run."
        return 1
    fi
    echo "no tailnet node is holding '$machine'"
    return 0
}

# --------------------------------------------------------------------------
# render_plan <machine> <rg> <location> <vm-size> <image> <vault> <template>
# --------------------------------------------------------------------------
render_plan() {
    local machine="$1" rg="$2" location="$3" vm_size="$4" image="$5" vault="$6" template="$7"
    cat <<EOF
  1. resource group    $rg  ($location)
  2. deployment        $template
     machine           $machine
     sku               $vm_size  (${DR_VCPUS_NEEDED} vCPU)
     image             ${image:-<unset>}
     tailnet tag       $DR_SERVER_TAG      <-- NOT tag:coord-worker
  3. ACL requirements  $DR_SERVER_TAG <- autogroup:member : 22,7433,7434,7435
                       $DR_SERVER_TAG <- tag:coord-worker : 7435
                       $DR_SERVER_TAG -> tag:coord-worker : 7433  (the daemon dials OUT)
  4. boot path         provision-server.sh --vault $vault
EOF
    # Aligned under step 4's own text (5 spaces, matching how step 2's
    # sub-items line up under "deployment" above): these are step 4's
    # sub-items, and they come from provision-server.sh's own table rather
    # than a second copy here.
    dr_server_plan "$vault" "$DR_SECRETS_FILE_DEFAULT" "/var/lib/coord/dr-promote.json" \
        | sed 's/^/     /'
    cat <<EOF
  5. teardown          ./dr-down.sh --machine $machine
EOF
}

# --------------------------------------------------------------------------
main() {
    # Subscription-specific IDs live outside the repo, alongside coord's other
    # state -- the same file the worker lane uses, because the vault, identity,
    # DNS zone and OAuth client are shared.
    local DR_ENV="${DR_ENV:-${EPIC_ENV:-$HOME/.coord/epic.env}}"

    # Parsed into *_ARG first: `source "$DR_ENV"` below assigns to whatever
    # name it finds, and epic.env legitimately sets LOCATION -- sourcing into
    # the same local would silently discard an explicit --location.
    local MACHINE="$DR_MACHINE_DEFAULT" LOCATION_ARG="" VM_SIZE="$DR_VM_SIZE_DEFAULT"
    local VAULT_ARG="" DRY_RUN=0 ACL_FILE="$DR_UP_HERE/tailnet-acl.hujson" SNAPSHOT=""

    while [[ $# -gt 0 ]]; do
        case $1 in
            --machine)  MACHINE="$2"; shift 2 ;;
            --location) LOCATION_ARG="$2"; shift 2 ;;
            --vm-size)  VM_SIZE="$2"; shift 2 ;;
            --vault)    VAULT_ARG="$2"; shift 2 ;;
            --acl-file) ACL_FILE="$2"; shift 2 ;;
            --snapshot) SNAPSHOT="$2"; shift 2 ;;
            --dry-run)  DRY_RUN=1; shift ;;
            *) echo "unknown option: $1" >&2; exit 2 ;;
        esac
    done

    if [[ -f "$DR_ENV" ]]; then
        # shellcheck source=/dev/null
        source "$DR_ENV"
    else
        echo "missing $DR_ENV -- populate it from bootstrap-shared.sh output" >&2
        exit 1
    fi
    # NOT `local LOCATION=...`: bash makes the name local BEFORE evaluating the
    # right-hand side, so `local X="${X}"` reads the empty new local, not the
    # value `source` just set. These stay plain assignments on purpose.
    LOCATION="${LOCATION_ARG:-${LOCATION:-$DR_LOCATION_DEFAULT}}"
    VAULT="${VAULT_ARG:-${KEY_VAULT_NAME:-}}"

    local RG="rg-coord-dr-${MACHINE}"
    local TEMPLATE="${EASY_AZURE_DIR:-}"
    TEMPLATE="${TEMPLATE:+${TEMPLATE%/}/$DR_SERVER_MODULE_REL}"

    log "gates (all of them run; nothing is created until every one passes)"
    local -a BLOCKERS=()
    local out

    run_gate() {  # run_gate <label> <fn> [args...]
        local label="$1"; shift
        if out="$("$@" 2>&1)"; then
            printf '  \033[32mPASS\033[0m  %s\n' "$label"
            [[ -n "$out" ]] && sed 's/^/        /' <<<"$out"
        else
            printf '  \033[31mBLOCK\033[0m %s\n' "$label"
            [[ -n "$out" ]] && sed 's/^/        /' <<<"$out"
            BLOCKERS+=("$label: $(head -1 <<<"$out")")
        fi
    }

    run_gate "shared resource IDs in $DR_ENV" check_env_vars
    run_gate "easy-azure server module" check_server_module "${EASY_AZURE_DIR:-}"
    run_gate "tailnet policy carries $DR_SERVER_TAG" check_acl_tag "$ACL_FILE"
    run_gate "Key Vault holds the server credential set" check_vault_secrets "$VAULT"
    run_gate "vCPU quota in $LOCATION" check_quota "$LOCATION" "$VM_SIZE" "$DR_VCPUS_NEEDED"
    run_gate "tailnet hostname '$MACHINE' is free" check_hostname_collision "$MACHINE"

    log "plan"
    render_plan "$MACHINE" "$RG" "$LOCATION" "$VM_SIZE" "${SOURCE_IMAGE_ID:-}" "$VAULT" "${TEMPLATE:-<EASY_AZURE_DIR unset>}"

    if (( ${#BLOCKERS[@]} > 0 )); then
        {
            echo
            echo "REFUSED -- ${#BLOCKERS[@]} gate(s) block, nothing was created:"
            printf '  - %s\n' "${BLOCKERS[@]}"
        } >&2
        exit 1
    fi

    if (( DRY_RUN )); then
        echo
        echo "--dry-run: every gate passed and NOTHING was created. Re-run without --dry-run to provision."
        return 0
    fi

    # ----------------------------------------------------------------------
    # From here on resources are created and cost money.
    # ----------------------------------------------------------------------
    log "1/4  deploy $RG"
    az group create -n "$RG" -l "$LOCATION" -o none
    cleanup_hint() {
        local rc=$?
        (( rc == 0 )) || {
            echo >&2
            echo "  dr-up failed AFTER deploying $RG -- the VM is running and billing." >&2
            echo "  Tear it down with:  ./dr-down.sh --machine $MACHINE --force" >&2
        }
        return $rc
    }
    trap cleanup_hint EXIT

    az deployment group create -g "$RG" --name "dr-${MACHINE}" \
        --template-file "$TEMPLATE" \
        --parameters \
            prefix=coord environment=prod name="dr" \
            owner="${OWNER:-}" costCenter="${COST_CENTER:-}" \
            machineName="$MACHINE" \
            tailnetTag="$DR_SERVER_TAG" \
            sourceImageId="$SOURCE_IMAGE_ID" \
            identityResourceId="$IDENTITY_RESOURCE_ID" \
            identityClientId="$IDENTITY_CLIENT_ID" \
            keyVaultUri="$KEY_VAULT_URI" \
            keyVaultResourceId="$KEY_VAULT_RESOURCE_ID" \
            privateDnsZoneId="$PRIVATE_DNS_ZONE_ID" \
            sshPublicKey="$(cat "${SSH_PUBLIC_KEY_FILE:-$HOME/.ssh/id_ed25519.pub}")" \
            gitEmail="${GIT_EMAIL:-}" \
            vmSize="$VM_SIZE" \
        -o none

    log "2/4  run the boot path on $MACHINE"
    local admin_user="${ADMIN_USER:-azureuser}"
    scp -q -o StrictHostKeyChecking=accept-new \
        "$DR_UP_HERE/provision-server.sh" "${admin_user}@${MACHINE}:/tmp/provision-server.sh"
    local -a boot_args=(--vault "$VAULT")
    [[ -n "$SNAPSHOT" ]] && boot_args+=(--snapshot "$SNAPSHOT")
    ssh -o StrictHostKeyChecking=accept-new "${admin_user}@${MACHINE}" \
        sudo bash /tmp/provision-server.sh "${boot_args[@]}"

    log "3/4  confirm the board answers"
    # #2096: the verdict comes from a request made AFTER the boot path returned,
    # not from the boot path's exit code alone.
    local health
    if ! health="$(curl -fsS --max-time 10 "http://${MACHINE}:7435/healthz" 2>/dev/null)"; then
        echo "  the boot path reported ready but GET http://${MACHINE}:7435/healthz did not answer." >&2
        exit 1
    fi
    echo "  $MACHINE:7435 answered: $health"

    trap - EXIT
    log "4/4  ready"
    cat <<EOF

  machine   $MACHINE   ($DR_SERVER_TAG)
  board     http://${MACHINE}:7435
  teardown  ./dr-down.sh --machine $MACHINE

  Clients are still pinned to the old board (that pin is D5). Re-point
  ~/.coord/client.toml on each machine, or apply an ACL/DNS alias, before
  expecting the fleet to follow.

EOF
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
