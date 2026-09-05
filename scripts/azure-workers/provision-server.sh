#!/usr/bin/env bash
# Bring a freshly-created Azure VM up as the coord BOARD DAEMON (#3130, rung D4
# of epic #3117 — the Domain-B recovery path, site loss).
#
#   sudo ./provision-server.sh --vault kv-coord-jd-prod            # real boot
#        ./provision-server.sh --vault kv-coord-jd-prod --dry-run  # rehearsal
#
# Runs ON the DR VM. `dr-up.sh` scp's it there and invokes it; cloud-init can
# invoke it too. It is the *boot path* half of D4: fetch the credentials a
# server needs (a worker image deliberately carries none of them), then hand
# over to `coord dr promote` for the restore + units + board verification, and
# refuse to advertise this host as ready unless that came back with a
# merge-capable GitHub token and a working restic path.
#
# ---------------------------------------------------------------------------
# Why this re-implements nothing
# ---------------------------------------------------------------------------
# The restore, the unit ordering, the credential *probes* and the final
# "is the board actually serving" verdict all live in `coord dr promote`
# (coord/dr_promote.py, rung D3). A second shell implementation of any of them
# would be a split brain waiting to happen (#2085), so this script's readiness
# verdict is *parsed out of promote's own JSON record* rather than
# re-established here. What is genuinely new is the Key Vault fetch: D3 assumes
# it is running on a host that already has its credentials; a VM created ten
# minutes ago does not.
#
# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
# Fetched from Key Vault through the VM's managed identity, exactly like the
# worker's three, and NEVER baked into an image. They land in a 0600 file under
# /run (tmpfs — it cannot survive into a disk image or a snapshot), and no
# secret value is ever passed as an argv element or printed. Only NAMES are
# reported.
#
# `az` is invoked with the *secret name*; the value comes back on stdout and is
# captured into a shell variable. Anything that would put a value on a command
# line (`--value`, `export FOO=$SECRET` in a logged command, `echo "$SECRET"`)
# is a bug this script's tests assert against.
set -euo pipefail

# Where fetched secrets land. /run is tmpfs: nothing written here reaches a
# disk image, a snapshot, or a backup. Overridable for tests only.
DR_SECRETS_FILE_DEFAULT="/run/coord-dr/secrets.env"

# The tailnet tag a DR board carries. Deliberately NOT tag:coord-worker: a
# worker may only reach out, a server must be reachable. See tailnet-acl.hujson.
DR_SERVER_TAG="tag:coord-server"

# --------------------------------------------------------------------------
# The canonical answer to "which Key Vault secrets does a coord SERVER need?"
#
# One table, three consumers: this script's boot-time fetch, `dr-up.sh`'s
# pre-creation "does the vault actually hold them" gate, and `preflight.sh`'s
# server role. They must not drift, so they all call these functions rather
# than carrying their own list.
#
# Columns: <key-vault-secret-name>|<env var it exports>|required|<group>
#
# The `required` column is what gates readiness. `git-push-identity` is
# optional because a merge-capable `github-token` already authenticates a
# push over HTTPS; a separate SSH deploy key is a nicety, not a blocker.
#
# `board-token` deliberately exports `COORD_SERVE_TOKEN`, NOT some
# `COORD_BOARD_TOKEN` of our own invention: the daemon's bearer token is
# resolved by `coord.serve_app.resolve_serve_token()`, which only ever reads
# `$COORD_SERVE_TOKEN` or `~/.coord/serve_token` (`check_board_token_credential`
# in coord/dr_promote.py is the consumer). A name that consumer never looks at
# would make this credential permanently report "missing" no matter how
# successfully it was fetched from Key Vault -- exactly the two-definitions-
# of-the-same-fact split brain #2085 exists to catch. See
# dr_persist_board_token() below for the other half: the env var only reaches
# THIS process, but the daemon systemd starts afterwards needs the token to
# survive into its own, unrelated process.
# --------------------------------------------------------------------------
dr_server_secret_table() {
    cat <<'EOF'
github-token|GITHUB_TOKEN|required|github
board-token|COORD_SERVE_TOKEN|required|board
restic-password|RESTIC_PASSWORD|required|restic
backup-repository|COORD_BACKUP_REPOSITORY|required|restic
azure-account-name|AZURE_ACCOUNT_NAME|required|restic
azure-account-key|AZURE_ACCOUNT_KEY|required|restic
tailscale-oauth-secret|TS_OAUTH_SECRET|required|tailnet
git-push-identity|COORD_GIT_PUSH_KEY|optional|git
EOF
}

# dr_secret_names [requiredness] [group] -> one secret name per line.
# No argument means "every secret".
dr_secret_names() {
    local want_req="${1:-}" want_group="${2:-}" name env req group
    while IFS='|' read -r name env req group; do
        [[ -n "$name" ]] || continue
        [[ -z "$want_req"   || "$req"   == "$want_req"   ]] || continue
        [[ -z "$want_group" || "$group" == "$want_group" ]] || continue
        echo "$name"
    done < <(dr_server_secret_table)
}

# dr_secret_env <secret-name> -> the environment variable it exports.
# Empty (and non-zero) for a name that is not in the table, so a typo in a
# caller cannot silently export nothing.
dr_secret_env() {
    local want="$1" name env req group
    while IFS='|' read -r name env req group; do
        [[ "$name" == "$want" ]] || continue
        echo "$env"; return 0
    done < <(dr_server_secret_table)
    return 1
}

# dr_secret_group <secret-name> -> its group, for the readiness message.
dr_secret_group() {
    local want="$1" name env req group
    while IFS='|' read -r name env req group; do
        [[ "$name" == "$want" ]] || continue
        echo "$group"; return 0
    done < <(dr_server_secret_table)
    return 1
}

# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

# dr_resolve_coord -> path to the coord binary on this host.
# Same resolution order epic-up.sh/epic-down.sh use over ssh: a non-login shell
# has no venv on PATH, so `command -v coord` alone finds nothing.
dr_resolve_coord() {
    local c
    for c in "${COORD_BIN:-}" "$HOME/.coord-venv/bin/coord" "$HOME/.local/bin/coord" \
             "/home/coord/.coord-venv/bin/coord" "$(command -v coord 2>/dev/null)"; do
        [[ -n "$c" && -x "$c" ]] && { echo "$c"; return 0; }
    done
    return 1
}

# dr_fetch_secrets <vault> <secrets-file>
#
# Writes an EnvironmentFile-shaped 0600 file and prints one status line per
# secret to stdout:  "obtained <name>" | "missing <name>".
#
# Values never reach stdout, stderr, or an argv. The only place a value exists
# outside this shell is the 0600 file.
dr_fetch_secrets() {
    local vault="$1" outfile="$2"
    local name env value

    install -d -m 0700 "$(dirname "$outfile")"
    # Truncate through a 0600 umask rather than creating then chmod'ing: a
    # world-readable window, however short, is a window.
    ( umask 0177; : > "$outfile" )

    while read -r name; do
        [[ -n "$name" ]] || continue
        env="$(dr_secret_env "$name")"
        # `--name` takes the SECRET's name, never its value. The value comes
        # back on stdout and goes straight into a variable.
        if value="$(az keyvault secret show --vault-name "$vault" --name "$name" \
                    --query value -o tsv 2>/dev/null)" && [[ -n "$value" ]]; then
            # printf with the value as an ARGUMENT to printf (not interpolated
            # into the format string, and not an argv of any external command --
            # printf is a bash builtin, so this never reaches /proc/*/cmdline).
            printf '%s=%s\n' "$env" "$value" >> "$outfile"
            echo "obtained $name"
        else
            echo "missing $name"
        fi
        unset value
    done < <(dr_secret_names)
}

# dr_persist_board_token <token-value> [target-file]
#
# The board token is the one credential in the table that has to outlive this
# process. Every other secret (github-token, restic-*) is only ever read from
# the environment WHILE `coord dr promote` is running -- the restore and the
# GitHub probe both happen inside that one invocation. The board token is
# different: `coord serve` is started as a systemd --user unit (`coord dr
# promote`'s start_units()), which does NOT inherit this script's exported
# environment -- `systemctl --user enable --now` talks to the user's systemd
# manager over its own bus connection, not this shell -- and it is
# `Restart=always`, so it will be re-exec'd by systemd again on every future
# crash or reboot, long after $COORD_SERVE_TOKEN has left every process that
# ever held it.
#
# `coord.serve_app.resolve_serve_token()`'s only source that survives that is
# `SERVE_TOKEN_FILE` (~/.coord/serve_token), so that is where it has to land.
# Exporting the env var (which the generic `set -a; source "$SECRETS_FILE"`
# step already does) covers `check_board_token_credential()`'s probe and
# `verify_board()`'s request inside *this* run of `coord dr promote`; this
# function covers every run after it.
#
# A no-op, not a failure, when the value is empty: the "secret was not
# obtained" blocker is already reported by dr_ready_blockers from the fetch
# step, and this function has nothing to persist in that case.
dr_persist_board_token() {
    local value="$1" target="${2:-$HOME/.coord/serve_token}"
    [[ -n "$value" ]] || return 0
    install -d -m 0700 "$(dirname "$target")"
    # umask, not chmod-after: a world-readable window, however short, is a
    # window (same reasoning as dr_fetch_secrets' own outfile).
    ( umask 0177; printf '%s' "$value" > "$target" )
}

# dr_obtained_csv <status-lines> -> comma-separated names that were obtained.
dr_obtained_csv() {
    local lines="$1" out="" status name
    while read -r status name; do
        [[ "$status" == "obtained" && -n "$name" ]] || continue
        out="${out:+$out,}$name"
    done <<< "$lines"
    echo "$out"
}

# dr_report_secrets <status-lines>
#
# The "report which credentials it obtained" half of #3130's acceptance
# criteria. Names and verdicts only -- there is deliberately no code path here
# that can print a value.
dr_report_secrets() {
    local lines="$1" status name req
    echo "  Key Vault secrets:"
    while read -r status name; do
        [[ -n "$name" ]] || continue
        req="$(dr_secret_group "$name" 2>/dev/null || echo "?")"
        if [[ "$status" == "obtained" ]]; then
            printf '    [ok]      %-24s (%s)\n' "$name" "$req"
        else
            printf '    [MISSING] %-24s (%s)\n' "$name" "$req"
        fi
    done <<< "$lines"
}

# --------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------

# dr_ready_blockers <obtained-csv> <github-verdict> <promote-outcome>
#
# Pure: prints one blocker per line, returns 1 when there is at least one.
# Everything it grades comes from an observation taken AFTER the work was done
# -- the fetch's own status lines, and `coord dr promote`'s record, which sets
# outcome=ok only after a GET /board against the started daemon (#2096).
#
# `unknown`/empty is deliberately NOT an alias for ok. A credential whose
# capability was never established is a blocker, because the permissive default
# is precisely the unreachable-failure shape #2096 rejects: a board that serves
# /board and cannot merge a PR is not a recovered fleet.
#
# The other two credentials #3117 pin 4 names -- the git push identity and the
# board token -- are gated through <outcome> rather than checked a second time
# here: `coord dr promote` carries non-forceable blockers for both
# (dr_promote.CRED_GIT_PUSH / CRED_BOARD_TOKEN), so it cannot report outcome=ok
# with either of them missing or incapable. Re-probing them in shell would be a
# second answer to a question that already has one (#2085).
dr_ready_blockers() {
    local obtained="$1" gh_verdict="${2:-}" outcome="${3:-}"
    local name found=0 n blocked=0

    while read -r name; do
        [[ -n "$name" ]] || continue
        found=0
        for n in ${obtained//,/ }; do
            [[ "$n" == "$name" ]] && { found=1; break; }
        done
        if (( ! found )); then
            echo "required secret '$name' was not obtained from Key Vault (exports \$$(dr_secret_env "$name"))"
            blocked=1
        fi
    done < <(dr_secret_names required)

    case "$gh_verdict" in
        ok) ;;
        ""|unknown)
            echo "merge capability of 'github-token' was never established (verdict=${gh_verdict:-none}) -- an unprobed credential is not a passing one"
            blocked=1 ;;
        *)
            echo "'github-token' is present but cannot merge (coord dr promote reports verdict=$gh_verdict)"
            blocked=1 ;;
    esac

    if [[ "$outcome" != "ok" ]]; then
        echo "coord dr promote did not report a serving board (outcome=${outcome:-none})"
        blocked=1
    fi

    return $(( blocked ))
}

# dr_advertise_ready <obtained-csv> <github-verdict> <promote-outcome>
#
# The gate. Prints READY only when dr_ready_blockers is empty; otherwise prints
# every blocker and returns non-zero, so a caller (dr-up.sh, cloud-init) sees a
# failure rather than a half-booted server that looks recovered.
dr_advertise_ready() {
    local blockers
    if blockers="$(dr_ready_blockers "$@")"; then
        echo "READY: this host is serving the board as $DR_SERVER_TAG, with a merge-capable GitHub token and a working restic path."
        return 0
    fi
    {
        echo "NOT READY -- refusing to advertise this host as the board:"
        while read -r line; do
            [[ -n "$line" ]] || continue
            echo "  - $line"
        done <<< "$blockers"
        echo
        echo "  A board that serves /board and cannot merge a PR is not a recovered"
        echo "  fleet (pin 4 of #3117). Fix the credential and re-run; do not force."
    } >&2
    return 1
}

# --------------------------------------------------------------------------
# Plan (what --dry-run prints)
# --------------------------------------------------------------------------
dr_server_plan() {
    local vault="$1" secrets_file="$2" record="$3"
    local name env req group
    echo "  tailnet tag        $DR_SERVER_TAG"
    echo "  key vault          ${vault:-<unset>}"
    echo "  secrets file       $secrets_file (tmpfs, 0600, never imaged)"
    echo "  promote record     $record"
    echo "  secrets to fetch:"
    while IFS='|' read -r name env req group; do
        [[ -n "$name" ]] || continue
        printf '    %-24s -> $%-26s %s (%s)\n' "$name" "$env" "$req" "$group"
    done < <(dr_server_secret_table)
    echo "  then               coord dr promote  (restore -> ROLE_DAEMON units -> GET /board)"
    echo "  ready gate         every 'required' secret + github-token verdict=ok + promote outcome=ok"
}

# --------------------------------------------------------------------------
main() {
    local VAULT="${KEY_VAULT_NAME:-}"
    local SECRETS_FILE="$DR_SECRETS_FILE_DEFAULT"
    local RECORD="/var/lib/coord/dr-promote.json"
    local DRY_RUN=0 SNAPSHOT="" BOARD_URL="" PROMOTE_FORCE=0

    while [[ $# -gt 0 ]]; do
        case $1 in
            --vault)        VAULT="$2"; shift 2 ;;
            --secrets-file) SECRETS_FILE="$2"; shift 2 ;;
            --record)       RECORD="$2"; shift 2 ;;
            --snapshot)     SNAPSHOT="$2"; shift 2 ;;
            --board-url)    BOARD_URL="$2"; shift 2 ;;
            # Passed straight through to `coord dr promote --force`, which
            # waives ONLY "the incumbent still answers" and "the local store is
            # non-empty". It never waives a missing credential -- see
            # dr_promote.Blocker.forceable.
            --force)        PROMOTE_FORCE=1; shift ;;
            --dry-run)      DRY_RUN=1; shift ;;
            *) echo "unknown option: $1" >&2; exit 2 ;;
        esac
    done

    [[ -n "$VAULT" ]] || { echo "usage: $0 --vault <key-vault-name> [--dry-run]" >&2; exit 2; }

    if (( DRY_RUN )); then
        echo "=== provision-server.sh --dry-run (nothing is fetched, nothing is started) ==="
        dr_server_plan "$VAULT" "$SECRETS_FILE" "$RECORD"
        return 0
    fi

    [[ $EUID -eq 0 ]] || { echo "must run as root (it writes $SECRETS_FILE and starts system units)" >&2; exit 1; }

    local coord
    coord="$(dr_resolve_coord)" || {
        echo "no coord binary on this host -- the image is not a coord image" >&2; exit 1; }

    echo "=== 1/4  fetch credentials from $VAULT (managed identity) ==="
    local status_lines obtained
    status_lines="$(dr_fetch_secrets "$VAULT" "$SECRETS_FILE")"
    obtained="$(dr_obtained_csv "$status_lines")"
    dr_report_secrets "$status_lines"

    # Load them into this process's environment for `coord dr promote`. `set -a`
    # exports without naming any value on a command line.
    set -a
    # shellcheck source=/dev/null
    source "$SECRETS_FILE"
    set +a

    # Persist the board token where `coord serve` will find it on every future
    # restart -- see dr_persist_board_token's own comment for why the export
    # two lines up is not enough on its own.
    dr_persist_board_token "${COORD_SERVE_TOKEN:-}"

    echo
    echo "=== 2/4  restore + start the daemon (coord dr promote) ==="
    # Built as an array so an empty option never collapses into a stray empty
    # argument, and so no value is word-split.
    local -a promote_args=(dr promote --record "$RECORD")
    [[ -n "$SNAPSHOT" ]] && promote_args+=(--snapshot "$SNAPSHOT")
    [[ -n "$BOARD_URL" ]] && promote_args+=(--board-url "$BOARD_URL")
    if (( PROMOTE_FORCE )); then promote_args+=(--force); fi

    # Deliberately NOT `|| true`: a failed promote must not reach the ready
    # gate looking like an absence of news. The record is still written on the
    # failure path, so the gate below can name the real reason.
    local promote_rc=0
    "$coord" "${promote_args[@]}" || promote_rc=$?

    echo
    echo "=== 3/4  read the verdict back out of $RECORD ==="
    local gh_verdict="" outcome=""
    if [[ -f "$RECORD" ]]; then
        gh_verdict="$(jq -r '.credentials["github-token"] // "unknown"' "$RECORD" 2>/dev/null || echo unknown)"
        outcome="$(jq -r '.outcome // "unknown"' "$RECORD" 2>/dev/null || echo unknown)"
        echo "  outcome=$outcome  github-token=$gh_verdict  (coord dr promote exit=$promote_rc)"
    else
        echo "  $RECORD was not written -- coord dr promote produced no verdict (exit=$promote_rc)" >&2
    fi

    echo
    echo "=== 4/4  readiness ==="
    dr_advertise_ready "$obtained" "$gh_verdict" "$outcome"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
