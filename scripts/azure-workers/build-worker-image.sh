#!/usr/bin/env bash
# Build the coord worker golden image and publish it as an Azure Compute
# Gallery image version. Run from your workstation.
#
#   ./build-worker-image.sh --rg rg-coord-images --gallery sigcoord [--seed-cargo-target]
#
# Produces:  /subscriptions/.../galleries/<gallery>/images/coord-worker/versions/<YYYY.MMDD.N>
# which is the `sourceImageId` the per-epic Bicep module deploys from. By
# default this script also WRITES that ID into $EPIC_ENV (default
# ~/.coord/epic.env) so the very next epic-up.sh picks it up with no manual
# edit (#1800) -- pass --no-update-env to publish without adopting, e.g. to
# bake a version you want to test before pointing epic-up.sh at it.
#
# The builder is throwaway and short-lived, so it gets a public IP with SSH
# locked to your current egress address. Worker VMs never do -- they are
# no-public-IP + NAT Gateway, reachable only over the tailnet.
set -euo pipefail

log() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

# Rewrite (or append) SOURCE_IMAGE_ID=$image_id in $EPIC_ENV so the next
# epic-up.sh deploys from the version this run just published, instead of
# silently redeploying whatever was pinned before (#1800). Kept as a
# standalone function, called from main(), so it can be exercised directly
# under pytest without touching az/ssh -- see
# tests/test_build_worker_image_env_update.py.
update_epic_env() {
    local version="$1" image_id="$2"

    if (( ! UPDATE_ENV )); then
        log "--no-update-env: leaving $EPIC_ENV untouched"
        echo "  Adopt it by hand when ready:  SOURCE_IMAGE_ID=$image_id"
        return 0
    fi

    if [[ ! -f "$EPIC_ENV" ]]; then
        echo "  note: $EPIC_ENV does not exist yet -- not auto-updating." >&2
        echo "  Create it from bootstrap-shared.sh output, then set:" >&2
        echo "    SOURCE_IMAGE_ID=$image_id" >&2
        return 0
    fi

    cp -p "$EPIC_ENV" "${EPIC_ENV}.bak"

    local tmp
    tmp="$(mktemp "${EPIC_ENV}.XXXXXX")"
    if grep -q '^SOURCE_IMAGE_ID=' "$EPIC_ENV"; then
        sed "s|^SOURCE_IMAGE_ID=.*|SOURCE_IMAGE_ID=$image_id|" "$EPIC_ENV" > "$tmp"
    else
        cp "$EPIC_ENV" "$tmp"
        printf 'SOURCE_IMAGE_ID=%s\n' "$image_id" >> "$tmp"
    fi
    chmod --reference="$EPIC_ENV" "$tmp"
    mv "$tmp" "$EPIC_ENV"

    log "$EPIC_ENV updated"
    echo "  SOURCE_IMAGE_ID=$image_id"
    echo "  (previous contents backed up to ${EPIC_ENV}.bak)"
}

# HARD-FAIL if the image definition being reused does not declare NVMe.
#
# `DiskControllerTypes` is IMMUTABLE after creation: it cannot be added later,
# and a version cannot be copied into a definition whose features differ.
# Omitting NVMe yields a SCSI-only image that v6/v7 SKUs (Dasv7 included)
# refuse to boot from, with a confusing "cannot boot with OS image or disk"
# error at DEPLOY time -- half an hour after the build reported success, on a
# VM you are paying for. This is the first gotcha in docs/EPHEMERAL_WORKERS.md
# and it is an `exit 1`, never a warning.
#
# A standalone function, like update_epic_env above, so
# tests/test_provision_core.py can drive both verdicts for real against a stub
# `az` -- a guard whose failing branch is never executed is not a guard.
require_nvme_declared() {
    local rg="$1" gallery="$2" image_def="$3" feat
    feat="$(az sig image-definition show -g "$rg" --gallery-name "$gallery" \
             --gallery-image-definition "$image_def" \
             --query "features[?name=='DiskControllerTypes'].value | [0]" -o tsv 2>/dev/null)"
    # An EMPTY answer is a failure, not a pass: `az` erroring out, or a
    # definition with no features at all, must not fall through to "fine".
    if [[ "$feat" != *NVMe* ]]; then
        echo "ERROR: image definition '$image_def' declares DiskControllerTypes='${feat:-none}'." >&2
        echo "  Features are immutable. Delete the definition and its versions, then re-run:" >&2
        echo "    az sig image-version delete -g $rg --gallery-name $gallery --gallery-image-definition $image_def --gallery-image-version <ver>" >&2
        echo "    az sig image-definition delete -g $rg --gallery-name $gallery --gallery-image-definition $image_def" >&2
        return 1
    fi
    return 0
}

main() {
RG=""; GALLERY=""; LOCATION="eastus"; IMAGE_DEF="coord-worker"
VM_SIZE="Standard_D8as_v7"; OS_DISK_GB=128; ADMIN_USER="azureuser"
PROVISION_ARGS=""
BUILDER="coord-img-builder-$$"
# Subscription-specific IDs live outside the repo -- same file and override
# convention as epic-up.sh/epic-down.sh.
EPIC_ENV="${EPIC_ENV:-$HOME/.coord/epic.env}"
UPDATE_ENV=1

while [[ $# -gt 0 ]]; do
    case $1 in
        --rg)                RG="$2"; shift 2 ;;
        --gallery)           GALLERY="$2"; shift 2 ;;
        --location)          LOCATION="$2"; shift 2 ;;
        --vm-size)           VM_SIZE="$2"; shift 2 ;;
        --seed-cargo-target) PROVISION_ARGS+=" --seed-cargo-target"; OS_DISK_GB=256; shift ;;
        --with-gtk)          PROVISION_ARGS+=" --with-gtk"; shift ;;
        --with-browser)      PROVISION_ARGS+=" --with-browser"; shift ;;
        --no-update-env)     UPDATE_ENV=0; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ -n "$RG" && -n "$GALLERY" ]] || { echo "usage: $0 --rg <rg> --gallery <gallery>" >&2; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cleanup() {
    if [[ "${KEEP_BUILDER:-0}" == "1" ]]; then
        log "KEEP_BUILDER=1 -- leaving $BUILDER for inspection"; return
    fi
    log "cleanup: deleting builder $BUILDER"
    # `az vm delete` returns before ARM has finished releasing the disk and
    # NIC, so a single immediate pass silently leaves a 128GB Premium OS disk
    # (~$20/mo) and the auto-created VNet behind. Retry until they actually go.
    az vm delete -g "$RG" -n "$BUILDER" --yes --force-deletion true 2>/dev/null || true
    for attempt in 1 2 3 4 5 6; do
        az network nic       delete -g "$RG" -n "${BUILDER}VMNic"    2>/dev/null || true
        az network public-ip delete -g "$RG" -n "${BUILDER}PublicIP" 2>/dev/null || true
        az network nsg       delete -g "$RG" -n "${BUILDER}NSG"      2>/dev/null || true
        for d in $(az disk list -g "$RG" --query "[?starts_with(name,'${BUILDER}')].name" -o tsv 2>/dev/null); do
            az disk delete -g "$RG" -n "$d" --yes 2>/dev/null || true
        done
        az network vnet delete -g "$RG" -n "${BUILDER}VNET" 2>/dev/null || true

        leftover="$(az resource list -g "$RG" \
            --query "[?starts_with(name,'${BUILDER}')].name" -o tsv 2>/dev/null | wc -l)"
        (( leftover == 0 )) && { echo "  cleanup complete"; break; }
        (( attempt == 6 )) && {
            echo "  WARNING: $leftover builder resource(s) still present in $RG." >&2
            echo "  A leftover OS disk bills ~\$20/mo — delete by hand:" >&2
            az resource list -g "$RG" --query "[?starts_with(name,'${BUILDER}')].name" -o tsv >&2
            break
        }
        sleep 15
    done
}
trap cleanup EXIT

# --------------------------------------------------------------------------
log "0/6  preflight"
az account show --query '{sub:name, id:id}' -o tsv
MY_IP="$(curl -fsS https://api.ipify.org)"
echo "SSH will be restricted to $MY_IP/32"

az group create -n "$RG" -l "$LOCATION" -o none
az sig create -g "$RG" --gallery-name "$GALLERY" -l "$LOCATION" -o none 2>/dev/null || true
# DiskControllerTypes MUST be declared here. It is IMMUTABLE after creation --
# it cannot be added later, and a version cannot be copied into a definition
# whose features differ. Omitting NVMe yields a SCSI-only image that v6/v7 SKUs
# (Dasv7 included) refuse to boot from, with a confusing "cannot boot with OS
# image or disk" error at deploy time rather than at build time.
az sig image-definition create \
    -g "$RG" --gallery-name "$GALLERY" --gallery-image-definition "$IMAGE_DEF" \
    --publisher coord --offer coord-worker --sku ubuntu-2404-x64 \
    --os-type Linux --os-state generalized \
    --hyper-v-generation V2 --architecture x64 \
    --features "DiskControllerTypes=SCSI,NVMe SecurityType=TrustedLaunchSupported" \
    -o none 2>/dev/null || true

# Fail loudly if an OLD definition without NVMe is being reused -- otherwise the
# build succeeds and only the first epic-up discovers the image is unusable.
require_nvme_declared "$RG" "$GALLERY" "$IMAGE_DEF" || exit 1

# --------------------------------------------------------------------------
log "1/6  create builder ($VM_SIZE, ${OS_DISK_GB}GB)"
az vm create -g "$RG" -n "$BUILDER" \
    --image Canonical:ubuntu-24_04-lts:server:latest \
    --size "$VM_SIZE" --os-disk-size-gb "$OS_DISK_GB" \
    --admin-username "$ADMIN_USER" --generate-ssh-keys \
    --public-ip-sku Standard --nsg-rule NONE -o none
# `az vm open-port` cannot restrict a source range (it has no
# --source-address-prefixes flag), and an unrestricted :22 on a public IP is
# not acceptable even for a short-lived builder. Create the rule directly.
NSG_NAME="$(az vm show -g "$RG" -n "$BUILDER" \
    --query 'networkProfile.networkInterfaces[0].id' -o tsv 2>/dev/null \
  | xargs -r -I{} az network nic show --ids {} \
    --query 'networkSecurityGroup.id' -o tsv 2>/dev/null)"
NSG_NAME="${NSG_NAME##*/}"
[[ -n "$NSG_NAME" ]] || NSG_NAME="${BUILDER}NSG"   # az vm create's default name
az network nsg rule create -g "$RG" --nsg-name "$NSG_NAME" -n AllowSshFromOperator \
    --priority 1000 --direction Inbound --access Allow --protocol Tcp \
    --source-address-prefixes "$MY_IP/32" --source-port-ranges '*' \
    --destination-address-prefixes '*' --destination-port-ranges 22 -o none
IP="$(az vm show -d -g "$RG" -n "$BUILDER" --query publicIps -o tsv)"
echo "builder at $IP"

SSH="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 ${ADMIN_USER}@${IP}"
for i in {1..30}; do $SSH true 2>/dev/null && break; sleep 10; done
$SSH true || { echo "builder never became reachable" >&2; exit 1; }

# --------------------------------------------------------------------------
log "2/6  provision (10-20 min)"
# #3139: provision-worker.sh sources the SHARED provisioning core, so the
# builder needs it too. It goes to /tmp/lib/ — one of the two layouts
# provision-worker.sh looks in — and the script hard-fails rather than falling
# back to inlined values if it is missing, so a copy that silently did not
# happen cannot produce a half-provisioned image that reports success.
CORE_SRC="$HERE/../lib/provision-core.sh"
[[ -f "$CORE_SRC" ]] || { echo "cannot find $CORE_SRC — the shared provisioning core (#3139)" >&2; exit 1; }
$SSH "mkdir -p /tmp/lib"
scp -o StrictHostKeyChecking=accept-new \
    "$HERE/provision-worker.sh" "$HERE/scrub-and-generalize.sh" "${ADMIN_USER}@${IP}:/tmp/"
scp -o StrictHostKeyChecking=accept-new \
    "$CORE_SRC" "${ADMIN_USER}@${IP}:/tmp/lib/"
$SSH "test -f /tmp/lib/provision-core.sh" \
    || { echo "provision-core.sh did not land on the builder" >&2; exit 1; }
$SSH "chmod +x /tmp/provision-worker.sh /tmp/scrub-and-generalize.sh"
$SSH "sudo /tmp/provision-worker.sh${PROVISION_ARGS}"

# --------------------------------------------------------------------------
log "3/6  scrub + deprovision"
# waagent kills the session on success, so a non-zero exit here is expected.
$SSH "sudo /tmp/scrub-and-generalize.sh" || true
# Confirm it really deprovisioned rather than failing its own leak check.
$SSH "test ! -e /var/lib/tailscale/tailscaled.state" 2>/dev/null \
    || echo "note: builder no longer reachable (expected after deprovision)"

# --------------------------------------------------------------------------
log "4/6  deallocate + generalize"
az vm deallocate -g "$RG" -n "$BUILDER" -o none
az vm generalize -g "$RG" -n "$BUILDER" -o none

# --------------------------------------------------------------------------
log "5/6  publish image version"
# Gallery versions must be strictly increasing X.Y.Z integers -- date-derived
# with a patch counter so several builds a day don't collide.
DATE_PART="$(date -u +%Y).$(date -u +%m%d)"
PATCH=0
while az sig image-version show -g "$RG" --gallery-name "$GALLERY" \
        --gallery-image-definition "$IMAGE_DEF" \
        --gallery-image-version "${DATE_PART}.${PATCH}" -o none 2>/dev/null; do
    PATCH=$((PATCH + 1))
done
VERSION="${DATE_PART}.${PATCH}"

SRC_ID="$(az vm show -g "$RG" -n "$BUILDER" --query id -o tsv)"
az sig image-version create \
    -g "$RG" --gallery-name "$GALLERY" \
    --gallery-image-definition "$IMAGE_DEF" --gallery-image-version "$VERSION" \
    --virtual-machine "$SRC_ID" \
    --target-regions "${LOCATION}=1" \
    --replica-count 1 -o none

IMAGE_ID="$(az sig image-version show -g "$RG" --gallery-name "$GALLERY" \
    --gallery-image-definition "$IMAGE_DEF" --gallery-image-version "$VERSION" \
    --query id -o tsv)"

# --------------------------------------------------------------------------
log "6/6  done"
update_epic_env "$VERSION" "$IMAGE_ID"
cat <<EOF

  Image version: $VERSION
  sourceImageId: $IMAGE_ID

  Rebuild when: a coord release lands (agent-side changes need a fresh venv),
  the Claude Code CLI updates, or the crate cache goes stale enough that the
  first build re-fetches most of the registry.

EOF
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
