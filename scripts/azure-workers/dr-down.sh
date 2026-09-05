#!/usr/bin/env bash
# Tear down the DR board server, safely (#3130, rung D4 of epic #3117).
#
#   ./dr-down.sh --machine coord-dr            # drain first, then delete
#   ./dr-down.sh --machine coord-dr --force    # skip the drain, destroy anyway
#
# Order matters, for exactly the reason epic-down.sh spells out: deleting the
# resource group under a running host loses any work that has not been pushed.
# So stop routing NEW work, wait for in-flight work to finish, check nobody is
# driving an interactive session on it, and only then delete.
#
# What is different from epic-down.sh: this host is (or was) the *board*, not a
# worker. There is no coordinator.yml entry to remove -- the machine being torn
# down is the one that was serving the config. Deleting it while the fleet is
# still pointed at it is a total outage rather than one fewer worker, so the
# drain refusals here are the ones that matter and `--force` is the only way
# past them.
set -euo pipefail

DR_DRAIN_TIMEOUT_DEFAULT=3600

log() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

# --------------------------------------------------------------------------
# dr_resolve_coord_remote -- the snippet run ON the target host to find coord.
# Same resolution order epic-up.sh/epic-down.sh use: `ssh host 'cmd'` runs a
# NON-login shell, so the venv coord lives in is not on PATH.
# --------------------------------------------------------------------------
dr_remote_pause() {
    local host="$1" machine="$2"
    ssh "$host" bash -euo pipefail -s -- "$machine" <<'REMOTE'
MACHINE="$1"
COORD=""
for c in "${COORD_BIN:-}" "$HOME/.coord-venv/bin/coord" "$HOME/.local/bin/coord" "$(command -v coord 2>/dev/null)"; do
    [[ -n "$c" && -x "$c" ]] && { COORD="$c"; break; }
done
[[ -n "$COORD" ]] || { echo "coord not found on this host" >&2; exit 1; }
"$COORD" pause "$MACHINE"
REMOTE
}

# dr_active_count <machine> -> the agent's own `active` (running assignment
# count), or the literal string "unreachable".
#
# /health is the authoritative per-machine signal; the board can lag it, and on
# a DR host the board IS this machine, so asking the board about itself is
# worse than useless.
dr_active_count() {
    local machine="$1" health
    health="$(curl -fsS --max-time 5 "http://${machine}:7433/health" 2>/dev/null || true)"
    if [[ -z "$health" ]]; then
        echo "unreachable"; return 0
    fi
    jq -r '.active // 0' <<<"$health" 2>/dev/null || echo "unreachable"
}

# dr_wait_for_drain <machine> <timeout-seconds>
#
# Returns 0 once the agent reports active=0 (or is already gone), 1 on timeout.
# The verdict comes from a /health read taken after each wait, never from the
# pause call having not thrown (#2096).
dr_wait_for_drain() {
    local machine="$1" timeout="$2" active
    local deadline=$(( SECONDS + timeout ))
    while true; do
        active="$(dr_active_count "$machine")"
        if [[ "$active" == "unreachable" ]]; then
            echo "  agent unreachable -- assuming already down"
            return 0
        fi
        if [[ "$active" == "0" ]]; then
            echo "  idle (active=0)"
            return 0
        fi
        if (( SECONDS >= deadline )); then
            echo "  still $active assignment(s) running after ${timeout}s." >&2
            echo "  Re-run with --force to destroy anyway, or 'coord stop <id>' first." >&2
            return 1
        fi
        printf '  active=%s, waiting...\n' "$active"
        sleep 30
    done
}

# dr_live_sessions <machine> -> prints any live interactive session names.
#
# Interactive tmux sessions are invisible to /health's assignment count, so a
# testing or merge agent someone is driving by hand would be killed silently.
#
# `coord sessions --remote --json` (coord/commands/sessions.py, sessions_cmd)
# emits a top-level OBJECT -- `{"sessions": [...]}` -- with each entry keyed
# `session_name`, never a bare array keyed `name`/`session`. Parse the real
# shape, not an assumed one: a `type=="array"` guard against that object is
# always false, so it always took the `else empty` branch and reported no
# sessions no matter what was actually running -- silently defeating the one
# check this function exists to make.
dr_live_sessions() {
    local machine="$1" sessions
    sessions="$(coord sessions --remote --json 2>/dev/null || true)"
    [[ -n "$sessions" ]] || return 0
    jq -r --arg m "$machine" \
        '.sessions[]? | select((.machine // "")==$m) | (.session_name // "?")' \
        <<<"$sessions" 2>/dev/null || true
}

# dr_group_state <rg> -> the resource group's provisioningState, or "absent".
dr_group_state() {
    local rg="$1" state
    state="$(az group show -n "$rg" --query properties.provisioningState -o tsv 2>/dev/null || true)"
    echo "${state:-absent}"
}

# --------------------------------------------------------------------------
main() {
    local MACHINE="coord-dr" RG="" FORCE=0
    local DRAIN_TIMEOUT="$DR_DRAIN_TIMEOUT_DEFAULT" DRY_RUN=0
    local PAUSE_HOST=""

    while [[ $# -gt 0 ]]; do
        case $1 in
            --machine)       MACHINE="$2"; shift 2 ;;
            --rg)            RG="$2"; shift 2 ;;
            --drain-timeout) DRAIN_TIMEOUT="$2"; shift 2 ;;
            --pause-host)    PAUSE_HOST="$2"; shift 2 ;;
            --force)         FORCE=1; shift ;;   # skip the drain wait, destroy anyway
            --dry-run)       DRY_RUN=1; shift ;;
            *) echo "unknown option: $1" >&2; exit 2 ;;
        esac
    done

    RG="${RG:-rg-coord-dr-${MACHINE}}"
    # The DR server runs its own daemon, so `coord pause` belongs on the DR
    # host itself -- it is the board. Overridable for the case where the board
    # has already been moved elsewhere.
    PAUSE_HOST="${PAUSE_HOST:-$MACHINE}"

    local state
    state="$(dr_group_state "$RG")"
    if [[ "$state" == "absent" ]]; then
        echo "$RG does not exist -- nothing to do"
        return 0
    fi

    if (( DRY_RUN )); then
        cat <<EOF
=== dr-down.sh --dry-run (nothing is paused, drained or deleted) ===
  machine        $MACHINE
  resource group $RG  (provisioningState=$state)
  would: 1. coord pause $MACHINE  on $PAUSE_HOST
         2. wait for http://${MACHINE}:7433/health active=0 (timeout ${DRAIN_TIMEOUT}s)
         3. refuse if any live interactive session is on $MACHINE
         4. az group delete -n $RG --yes --no-wait
         5. re-read provisioningState to confirm deletion actually started
  --force skips steps 2 and 3. Unpushed work on this VM would be LOST.
EOF
        return 0
    fi

    # ----------------------------------------------------------------------
    log "1/5  stop routing new work to $MACHINE"
    # `coord pause` explicitly does NOT cancel in-flight assignments -- that is
    # what makes it the right call here.
    if ! dr_remote_pause "$PAUSE_HOST" "$MACHINE"; then
        echo "  pause FAILED -- new work may still route to $MACHINE. Investigate before continuing." >&2
        exit 1
    fi
    echo "  paused"

    # ----------------------------------------------------------------------
    log "2/5  drain in-flight work"
    if (( FORCE )); then
        echo "  --force: skipping drain. Unpushed work on this VM will be LOST."
    else
        dr_wait_for_drain "$MACHINE" "$DRAIN_TIMEOUT" || exit 1
    fi

    # ----------------------------------------------------------------------
    log "3/5  check for live interactive sessions"
    if (( FORCE )); then
        echo "  --force: not checking. A session someone is driving will be killed."
    else
        local live
        live="$(dr_live_sessions "$MACHINE")"
        if [[ -n "$live" ]]; then
            {
                echo "  WARNING: live interactive session(s) on $MACHINE:"
                sed 's/^/    /' <<<"$live"
                echo "  Finish or detach them, or re-run with --force."
            } >&2
            exit 1
        fi
        echo "  none"
    fi

    # ----------------------------------------------------------------------
    log "4/5  delete $RG"
    # Takes the VM, OS disk, NIC, NSG, VNet, NAT Gateway, public IP, the Key
    # Vault private endpoint and its DNS zone link -- everything billable.
    az group delete -n "$RG" --yes --no-wait -o none

    # ----------------------------------------------------------------------
    log "5/5  confirm deletion actually started"
    # #2096: `az group delete --no-wait` returning 0 only proves the request
    # was accepted. Re-read the state so the line an operator remembers is an
    # observation, not an assumption.
    state="$(dr_group_state "$RG")"
    case "$state" in
        Deleting|absent) echo "  $RG is now '$state' -- deletion is under way" ;;
        *)
            echo "  $RG still reports provisioningState='$state' after the delete request." >&2
            echo "  The group is NOT known to be deleting; check the Azure portal before" >&2
            echo "  assuming this VM stopped billing." >&2
            exit 1 ;;
    esac

    cat <<EOF

  $MACHINE torn down; $RG deleting.

  The tailnet node removes itself if it joined with ephemeral=true; otherwise
  remove it by hand in the admin console, or the next dr-up.sh will fail fast
  on the hostname collision (which is the point).

  Verify:  az group show -n $RG   (should 404 shortly)

EOF
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
