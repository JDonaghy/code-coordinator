#!/usr/bin/env bash
# Take a freshly installed OS to a working fleet member (#3138).
#
#   ./scripts/provision-machine.sh --role thin-client|worker|server [--machine NAME]
#
# THE CONTRACT IS THE DOCTOR, NOT THE PHASE COUNT.
# -----------------------------------------------
# This script's job is "make `coord machine doctor` green for this role", not
# "run 12 phases without erroring". Every phase is idempotent and re-runnable,
# and the last phase invokes the doctor and exits non-zero if it is not clean.
# That is what buys resumability for free: when phase 9 of 12 dies three hours
# into a rebuild you fix the cause and re-run the WHOLE script rather than
# reconstructing where you were. It is also why #3137 (the toolchain and
# identity layers) had to land first — without them the gate passes machines
# that cannot work.
#
# ROLES
# -----
#   thin-client  a registered fleet member that runs no work: no toolchains,
#                no repo clones, no daemon units.
#   worker       + toolchains for its declared capabilities, repo clones,
#                the graph.
#   server       + the ten daemon units in `coord/deploy_manifest.py`'s
#                ROLE_UNITS[daemon], the backup credential, the SSD store.
#
# The doctor knows exactly TWO roles (#3128: `worker` and `daemon`), and
# `coord/machine_onboard.py` says in as many words that *"the 'thin client'
# column of #3137's own table is the `worker` (default) role"*. So this script
# grades thin-client and worker against `worker`, and server against `daemon`,
# and writes the matching value into `~/.coord/role`.
#
# WHY A THIN CLIENT STILL GETS `coord-agent`
# ------------------------------------------
# Deliberate, and forced by the gate rather than chosen around it:
# `machine_onboard.evaluate_network` raises `network.agent_unreachable` at
# CRIT for ANY machine that has a `machines[]` entry and no agent answering
# /health. A thin client that is registered (which #3138's own phase table
# asks for) and has no agent can therefore never be doctor-clean. Rather than
# teach this script to forgive a CRIT — a gate you can talk out of failing is
# not a gate — the thin-client role installs the agent too, and registers with
# NO capabilities and NO repos so layers 4/5/7 have nothing to demand of it and
# routing never sends it work. If the fleet ever wants a genuinely agent-less
# client, that is a third role in `deploy_manifest.ROLE_UNITS` + a doctor
# dimension, i.e. #3128/#3137 territory, not a special case here.
#
# NOT AZURE, AND NEVER A SECOND USER
# ----------------------------------
# `scripts/azure-workers/provision-worker.sh` is the golden-IMAGE builder: it
# runs as root, creates a dedicated `coord` user (because `waagent
# -deprovision+user` deletes the provisioning user's home), and installs zero
# identity on purpose. All three are wrong for bare metal, where the operator
# IS the user and identity is the whole point. This script therefore runs as
# the unprivileged operator (it refuses to run as root), uses `sudo` only for
# apt, and never creates an account. Converging the two is M3, deliberately
# not this issue.
#
# NOT macOS / NOT WSL. Ubuntu-first. The seam is systemd: every
# `systemctl --user` call below is `launchctl`/`launchd` on a Mac and a
# `wsl.conf`-started supervisor under WSL. `docs/MAC_MINI.md` and
# `docs/WSL_WINDOWS_WORKER.md` stay the runbooks for those.
#
# NOT a restore. Rebuilding a daemon host does NOT recover `coord.db` — that
# is `docs/DISASTER_RECOVERY.md` and #3129 (`coord dr promote`). This script
# points at them and deliberately duplicates none of it.

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"

# The shared provisioning core (#3139) — the ONE place the gh floor, the node
# major, the opencode pin, the rust location, the base package list and the
# repo clone list exist across both lanes. This script must never restate one
# of them; tests/test_provision_core.py greps both lanes and fails on a second
# literal. The core assumes nothing about privilege or user: what stays here
# is this lane's substrate (the operator's own account, sudo for packages
# only, identity front-loaded and interactive).
COORD_CORE_SUDO="sudo"
COORD_PROVISION_CORE="${COORD_PROVISION_CORE:-$SCRIPT_DIR/lib/provision-core.sh}"
[[ -f "$COORD_PROVISION_CORE" ]] || {
    printf 'provision-machine: cannot find the shared core at %s\n' "$COORD_PROVISION_CORE" >&2
    exit 1
}
# shellcheck source=lib/provision-core.sh
. "$COORD_PROVISION_CORE"

ROLE=""
MACHINE_NAME=""
HOST_NAME=""
CAPABILITIES=""
REPOS=""
AGENT_PORT=7433
DRY_RUN=0
ASSUME_YES="${COORD_PROVISION_ASSUME_YES:-0}"

# Overridable so the black-box suite can drive the whole script without a
# network, a systemd, or a real venv. Defaults are the production paths.
INSTALL_AGENT="${COORD_PROVISION_INSTALL_AGENT:-$REPO_ROOT/install-agent.sh}"
VENV_DIR="${COORD_PROVISION_VENV_DIR:-$HOME/.coord-venv}"
COORD_DIR="${COORD_PROVISION_COORD_DIR:-$HOME/.coord}"
SETTINGS_DIR="${COORD_SETTINGS_DIR:-$HOME/src/coord-settings}"
SRC_DIR="${COORD_PROVISION_SRC_DIR:-$HOME/src}"
SYSTEMD_USER_DIR="${COORD_PROVISION_SYSTEMD_DIR:-$HOME/.config/systemd/user}"
# Where the units' ExecStart= lines say the packaged helper scripts live:
# `coord-db-backup.service` runs `%h/.local/bin/coord-db-backup.sh`, and
# `%h` is systemd's specifier for the unit owner's home. Not the unit dir —
# see phase_daemon_units.
LOCAL_BIN_DIR="${COORD_PROVISION_LOCAL_BIN:-$HOME/.local/bin}"
# The documented production mountpoint (docs/AGENT_OPERATIONS.md's backup
# table, "target: /media/crucial/coord-backups/") is what
# coord/deploy/coord-db-backup.sh (unchanged by this script) hardcodes, so
# THAT is the default here too — not a placeholder path. Override only if a
# given host's SSD is genuinely mounted somewhere else.
BACKUP_MOUNT="${COORD_PROVISION_BACKUP_MOUNT:-/media/crucial}"

# The gh floor, the forge account and the repo list all come from the core —
# see the sourcing block above. Named here only so the phases below read the
# same as they did, and so $COORD_PROVISION_GITHUB_ORG still overrides.
GITHUB_ORG="${COORD_PROVISION_GITHUB_ORG:-$COORD_GITHUB_ORG}"
DEFAULT_REPOS="$(coord_core_repos_csv)"

# ── The phase table ──────────────────────────────────────────────────────────
#
# Data, not control flow, so `--dry-run` can print it and
# tests/test_provision_machine.py can assert on the ordering invariant that
# #3138 actually cares about:
#
#   EVERY interactive credential prompt happens in ONE phase, and that phase
#   runs BEFORE the first `slow` phase.
#
# "slow" means tens of minutes (rust toolchain builds, blob-filtered clones of
# four repos, cargo/npm cache warming) — the phases during which an operator
# reliably wanders off. The phases before `credentials` are marked `fast`
# because each is a hard PREREQUISITE of a prompt in it: you cannot
# `tailscale up` without the tailscale binary, `gh auth login` without gh,
# authenticate `claude` without node, or write a board token without a venv.
#
#   name | roles | speed
PHASE_TABLE=(
    "preflight|thin-client,worker,server|fast"
    "base-packages|thin-client,worker,server|fast"
    "cred-tools|thin-client,worker,server|fast"
    "coord-cli|thin-client,worker,server|fast"
    "role-declaration|thin-client,worker,server|fast"
    "credentials|thin-client,worker,server|interactive"
    "register|thin-client,worker,server|fast"
    "toolchains|worker,server|slow"
    "repos|worker,server|slow"
    "daemon-units|server|fast"
    "store|server|fast"
    "gate|thin-client,worker,server|fast"
)

# ── Output helpers ───────────────────────────────────────────────────────────

CHANGES=0
CURRENT_PHASE=""
# Units installed with an ExecStart that resolves to nothing (phase
# daemon-units). Advisory, never fatal — but reported in the final trailer,
# because neither `is-enabled` nor `coord machine doctor` can see this class.
DEAD_EXEC=0

log()      { printf '\n=== %s ===\n' "$*"; }
info()     { printf '    %s\n' "$*"; }
warn()     { printf '    ! %s\n' "$*" >&2; }
changed()  { CHANGES=$((CHANGES + 1)); printf 'CHANGE:   [%s] %s\n' "$CURRENT_PHASE" "$*"; }
unchanged() { printf 'NOCHANGE: [%s] %s\n' "$CURRENT_PHASE" "$*"; }
die()      { printf '\nprovision-machine: %s\n' "$*" >&2; exit 1; }

# Never interpolate a credential into any of the above. `ask_secret` below is
# the only function that touches one, and it writes it straight to a mode-0600
# file with a printf BUILTIN (so it never appears in argv / /proc either).

# ── Argument parsing ─────────────────────────────────────────────────────────

usage() {
    # Unquoted heredoc: the repo default is $DEFAULT_REPOS, from the shared
    # core (#3139), so the help text cannot say something the script does not
    # do. There is nothing else to interpolate in here — keep it that way.
    cat <<USAGE
usage: provision-machine.sh --role thin-client|worker|server [options]

  --role ROLE            required; thin-client | worker | server
  --machine NAME         board name for this machine (default: hostname -s)
  --host HOST            tailnet host for coordinator.yml (default: MagicDNS
                         FQDN if tailscale knows it, else the machine name)
  --capabilities CSV     capabilities to register + install toolchains for
                         (worker/server only; e.g. rust,gtk,browser)
  --repos CSV            repos to clone and register (worker/server only;
                         default: $DEFAULT_REPOS)
  --port N               agent port (default 7433)
  --yes                  do not pause for confirmation before the prompt block
  --dry-run              print the phase plan for this role and exit
  -h, --help             this

The script ends by running `coord machine doctor <machine> --ssh -v --role
<worker|daemon>` and exits non-zero if it is not clean. That verdict is the
whole contract; there is no flag to skip it.

Environment overrides (only needed on a host whose layout differs from the
documented production one):

  COORD_PROVISION_BACKUP_MOUNT   --role server only. Where the daemon's
                                 external backup SSD is mounted. Defaults to
                                 /media/crucial, the mountpoint
                                 coord/deploy/coord-db-backup.sh and
                                 docs/AGENT_OPERATIONS.md already document —
                                 override this ONLY if a given host's SSD is
                                 genuinely mounted somewhere else, not as a
                                 matter of course.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        # A value-taking flag with nothing after it (e.g. a bare trailing
        # `--role`) must not reach `shift 2` with only one positional
        # parameter left: bash's `shift` then fails outright and, under
        # `set -e`, aborts with its own unhelpful message instead of the
        # usage/die text below.
        --role|--machine|--host|--capabilities|--repos|--port)
            [[ $# -ge 2 ]] || { usage >&2; die "$1 requires a value"; }
            ;;
    esac
    case "$1" in
        --role)         ROLE="$2"; shift 2 ;;
        --machine)      MACHINE_NAME="$2"; shift 2 ;;
        --host)         HOST_NAME="$2"; shift 2 ;;
        --capabilities) CAPABILITIES="$2"; shift 2 ;;
        --repos)        REPOS="$2"; shift 2 ;;
        --port)         AGENT_PORT="$2"; shift 2 ;;
        --yes|-y)       ASSUME_YES=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

case "$ROLE" in
    thin-client|worker|server) ;;
    "") usage >&2; die "--role is required (thin-client | worker | server)" ;;
    *)  die "unknown --role '$ROLE' — expected thin-client, worker or server" ;;
esac

# The doctor's own role names (#3128 spells exactly two).
case "$ROLE" in
    server) DOCTOR_ROLE="daemon" ;;
    *)      DOCTOR_ROLE="worker" ;;
esac

[[ -n "$MACHINE_NAME" ]] || MACHINE_NAME="$(hostname -s)"
[[ -n "$MACHINE_NAME" ]] || die "could not determine a machine name — pass --machine"

if [[ "$ROLE" == "thin-client" ]]; then
    # A thin client is registered with nothing to run so that routing never
    # sends it work and layers 4/5/7 have nothing to demand — see the header.
    if [[ -n "$CAPABILITIES" || -n "$REPOS" ]]; then
        die "--capabilities/--repos are meaningless for --role thin-client: it is
registered with neither so the board never routes work to it. Use --role worker."
    fi
else
    [[ -n "$REPOS" ]] || REPOS="$DEFAULT_REPOS"
fi

# ── Phase plumbing ───────────────────────────────────────────────────────────

phase_names_for_role() {
    local role="$1" entry name roles
    for entry in "${PHASE_TABLE[@]}"; do
        name="${entry%%|*}"
        roles="${entry#*|}"; roles="${roles%%|*}"
        [[ ",$roles," == *",$role,"* ]] && printf '%s\n' "$name"
    done
    return 0
}

phase_speed() {
    local want="$1" entry name
    for entry in "${PHASE_TABLE[@]}"; do
        name="${entry%%|*}"
        if [[ "$name" == "$want" ]]; then printf '%s\n' "${entry##*|}"; return 0; fi
    done
    return 1
}

if [[ $DRY_RUN -eq 1 ]]; then
    printf 'PLAN: role=%s machine=%s doctor_role=%s\n' "$ROLE" "$MACHINE_NAME" "$DOCTOR_ROLE"
    n=0
    while IFS= read -r name; do
        n=$((n + 1))
        printf 'PLAN: %2d %-17s %s\n' "$n" "$name" "$(phase_speed "$name")"
    done < <(phase_names_for_role "$ROLE")
    printf 'PLAN: gate=coord machine doctor %s --ssh -v --role %s\n' "$MACHINE_NAME" "$DOCTOR_ROLE"
    exit 0
fi

have() { command -v "$1" >/dev/null 2>&1; }

# Run once per invocation: `apt-get install` against a stale/empty package
# index can fail with "unable to locate package" for reasons that have
# nothing to do with the real problem, on a fresh image where every
# BASE_REQUIREMENTS tool is already present (so phase_base_packages never ran
# its own `apt-get update`) but the index itself was never refreshed. Every
# later `apt-get install` in this script — restic, gh's own repo, gtk4, the
# browser pair — goes through this instead of calling `apt-get update` (or
# skipping it) on its own.
APT_UPDATED=0
apt_update_once() {
    [[ $APT_UPDATED -eq 1 ]] && return 0
    sudo apt-get update -qq
    APT_UPDATED=1
}

# `coord` from the venv this script installs, never whatever is on PATH: on a
# fresh box there is nothing on PATH, and after `install-agent.sh` the shim in
# ~/.local/bin may not be on the CURRENT shell's PATH yet.
coord_bin() {
    if [[ -x "$VENV_DIR/bin/coord" ]]; then printf '%s\n' "$VENV_DIR/bin/coord"
    elif have coord;                   then command -v coord
    else return 1; fi
}
coord_py() {
    if [[ -x "$VENV_DIR/bin/python" ]]; then printf '%s\n' "$VENV_DIR/bin/python"
    elif [[ -x "$VENV_DIR/bin/python3" ]]; then printf '%s\n' "$VENV_DIR/bin/python3"
    else return 1; fi
}

confirm_or_die() {
    # Used only for the "here is everything I am about to ask you for" pause.
    [[ "$ASSUME_YES" == "1" ]] && return 0
    local reply=""
    printf '    press Enter to continue, or Ctrl-C to abort: '
    read -r reply || true
}

# ── Phase 1: preflight ───────────────────────────────────────────────────────

phase_preflight() {
    [[ "$(id -u)" -ne 0 ]] || die "refusing to run as root.

This is a bare-metal installer, not the Azure golden-image builder. The
operator IS the fleet user here: ~/.coord-venv, ~/src and ~/.claude all live
in YOUR home directory, and creating a second account (which
scripts/azure-workers/provision-worker.sh does, because waagent deletes the
provisioning user) would put every one of them somewhere the agent never
looks. Re-run as your own login user; sudo is used only for apt."

    if have uname && [[ "$(uname -s)" != "Linux" ]]; then
        die "this script is Ubuntu-first and systemd-only. On macOS the seam is
launchd, not systemd — see docs/MAC_MINI.md; under WSL see
docs/WSL_WINDOWS_WORKER.md. Neither is handled here (#3138, out of scope)."
    fi

    if have python3; then
        coord_core_python_meets_floor \
            || warn "python3 is below $COORD_PYTHON_MIN_VERSION — the base-packages phase will try to fix it"
    fi

    if [[ -x "$INSTALL_AGENT" ]]; then
        unchanged "agent installer present at $INSTALL_AGENT"
    else
        die "cannot find install-agent.sh at $INSTALL_AGENT.

That script owns the venv layer (including the #2911 partial-venv trap) and
this one deliberately does not reimplement it. Clone code-coordinator first,
or set \$COORD_PROVISION_INSTALL_AGENT."
    fi

    unchanged "role=$ROLE machine=$MACHINE_NAME doctor-role=$DOCTOR_ROLE user=$(id -un)"
}

# ── Phase 2: base packages ───────────────────────────────────────────────────

# probe|apt-package, from the core ($COORD_BASE_REQUIREMENTS) — the same list
# the image lane installs from, so a package this fleet needs cannot be added
# to one lane and forgotten in the other. See the core for why the PROBE and
# not dpkg is the signal (#2911).
BASE_REQUIREMENTS=("${COORD_BASE_REQUIREMENTS[@]}")

_probe_ok() {
    local probe="$1"
    if [[ "$probe" == *" "* ]]; then
        # shellcheck disable=SC2086
        $probe >/dev/null 2>&1
    else
        command -v "$probe" >/dev/null 2>&1
    fi
}

phase_base_packages() {
    local entry probe pkg missing=()
    for entry in "${BASE_REQUIREMENTS[@]}"; do
        probe="${entry%%|*}"; pkg="${entry##*|}"
        if _probe_ok "$probe"; then
            unchanged "$probe"
        else
            info "missing: $probe -> apt package $pkg"
            [[ " ${missing[*]-} " == *" $pkg "* ]] || missing+=("$pkg")
        fi
    done

    if [[ ${#missing[@]} -eq 0 ]]; then
        unchanged "all base packages present"
    else
        apt_update_once
        DEBIAN_FRONTEND=noninteractive sudo apt-get install -y -qq --no-install-recommends "${missing[@]}" \
            || die "apt-get install failed for: ${missing[*]} — check apt sources/network and re-run
(this script is safe to re-run from the top)."
        changed "installed base packages: ${missing[*]}"
        # #2096: confirm AFTER the action. apt exiting 0 is not evidence the
        # tool resolves — a held package, a wrong suite, or a rename all exit
        # 0 and install nothing usable.
        for entry in "${BASE_REQUIREMENTS[@]}"; do
            probe="${entry%%|*}"
            _probe_ok "$probe" || die "apt reported success but '$probe' still does not work.
Fix the package source and re-run — this script is safe to re-run from the top."
        done
    fi

    coord_core_python_meets_floor \
        || die "python3 is below $COORD_PYTHON_MIN_VERSION; install-agent.sh enforces that floor
and will refuse. Install a $COORD_PYTHON_MIN_VERSION+ interpreter and re-run."
}

# ── Phase 3: the tools the credential prompts need ───────────────────────────

phase_cred_tools() {
    # tailscale — the binary only. `tailscale up` is a PROMPT and belongs in
    # the credentials phase, not here.
    if have tailscale; then
        unchanged "tailscale binary present"
    else
        curl -fsSL https://tailscale.com/install.sh | sh
        have tailscale || die "tailscale install reported success but the binary is not on PATH"
        changed "installed tailscale"
    fi

    # gh — the official repo and the floor both come from the core, which is
    # the same code provision-worker.sh step 2 runs. Only the wording of the
    # failure and the CHANGE/NOCHANGE bookkeeping are this lane's.
    local gh_ver=""
    gh_ver="$(coord_core_gh_version)"
    if coord_core_gh_meets_floor; then
        unchanged "gh $gh_ver (floor $COORD_GH_MIN_VERSION)"
    else
        info "installing gh from the official repo — Ubuntu's own gh is far below the $COORD_GH_MIN_VERSION floor"
        coord_core_install_gh \
            || die "apt-get install gh failed — check the github-cli apt source and network,
then re-run (this script is safe to re-run from the top)."
        # coord_core_install_gh runs its own `apt-get update` (a source list
        # just changed, so any earlier update in this run does not cover the
        # new repo's index) — record that so apt_update_once does not repeat it.
        APT_UPDATED=1
        # #2096: apt exiting 0 is not the verdict — re-probe, and treat a gh
        # that says nothing as a failure rather than as an unmeasured pass.
        gh_ver="$(coord_core_gh_version)"
        [[ -n "$gh_ver" ]] || die "gh install reported success but 'gh --version' produced nothing"
        coord_core_gh_meets_floor \
            || die "gh $gh_ver is still below the required $COORD_GH_MIN_VERSION floor"
        changed "installed gh $gh_ver"
    fi

    # node + the Claude Code CLI. Via nvm, into $HOME: install-agent.sh's node
    # shims (#1678) resolve an nvm install at RUN time, so a later `nvm
    # install` is a no-op for the agent's PATH. A root nodesource install
    # (what the golden image does) would work too but is not re-resolvable.
    # The MAJOR is $COORD_NODE_MAJOR either way — different installers,
    # substrate-forced; same node, because that is not substrate.
    if have node; then
        unchanged "node $(node --version 2>/dev/null || echo '?')"
    else
        export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
        [[ -s "$NVM_DIR/nvm.sh" ]] || curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
        # shellcheck disable=SC1091
        . "$NVM_DIR/nvm.sh"
        nvm install "$COORD_NODE_MAJOR"
        have node || die "nvm reported success but node is not on PATH"
        changed "installed node $(node --version)"
    fi

    if have claude; then
        unchanged "claude CLI $(claude --version 2>/dev/null | head -1)"
    else
        npm config set prefix "$HOME/.local"
        npm install -g @anthropic-ai/claude-code
        have claude || PATH="$HOME/.local/bin:$PATH" have claude \
            || die "npm reported success but the 'claude' CLI is not on PATH"
        changed "installed the Claude Code CLI"
    fi
}

# ── Phase 4: the coord CLI, the venv, the agent unit ─────────────────────────

phase_coord_cli() {
    # Delegated wholesale to install-agent.sh: it owns the venv (PyPI,
    # NEVER editable — the standing invariant in docs/AGENT_OPERATIONS.md),
    # the #2911 partial-venv recovery, the ~/.local/bin coord shim (#2936),
    # the node shims (#1678), the coord-agent unit and linger. This script
    # reimplements none of it and must never grow a `pip install` of its own.
    #
    # Skipped outright when the layer is already whole, because
    # install-agent.sh unconditionally does a `pip install --upgrade` and a
    # `systemctl restart` — real work, on a machine that needed none. This is
    # NOT the upgrade path (that is `coord agent update` / the
    # coord-release-propagate lane); it is a rebuild path, and a rebuild that
    # re-installs on every run is not the idempotent, safe-to-retry thing
    # #3138 asks for.
    local version=""
    version="$("$VENV_DIR/bin/coord" version 2>/dev/null || true)"
    if [[ -n "$version" ]] \
        && [[ -f "$SYSTEMD_USER_DIR/coord-agent.service" ]] \
        && [[ "$(systemctl --user is-enabled coord-agent 2>/dev/null || true)" == "enabled" ]]; then
        unchanged "coord venv $version, coord-agent.service installed and enabled"
        return 0
    fi

    "$INSTALL_AGENT" --machine "$MACHINE_NAME" --port "$AGENT_PORT"

    # #2096: install-agent.sh exiting 0 is not evidence the venv works — that
    # is the exact #2911 shape (a venv that exists with no usable pip).
    version="$("$VENV_DIR/bin/coord" version 2>/dev/null || true)"
    [[ -n "$version" ]] || die "install-agent.sh exited 0 but '$VENV_DIR/bin/coord version'
produces nothing — the venv is not usable. Delete $VENV_DIR and re-run."
    [[ -f "$SYSTEMD_USER_DIR/coord-agent.service" ]] \
        || die "install-agent.sh exited 0 but $SYSTEMD_USER_DIR/coord-agent.service does not exist"
    changed "installed the coord venv ($version) and coord-agent.service"
}

# ── Phase 5: the role declaration (#3128) ────────────────────────────────────

phase_role_declaration() {
    # `~/.coord/role` is #3128's host-local answer to "what am I?", and it has
    # to resolve with the board DOWN — which is exactly the DR case. Written
    # here rather than inferred anywhere later, and read back through
    # deploy_manifest.resolve_role so this script and the doctor cannot
    # disagree about what the file means.
    mkdir -p "$COORD_DIR"
    chmod 0700 "$COORD_DIR" 2>/dev/null || true
    local target="$COORD_DIR/role" current=""
    [[ -f "$target" ]] && current="$(tr -d '[:space:]' < "$target" 2>/dev/null || true)"
    if [[ "$current" == "$DOCTOR_ROLE" ]]; then
        unchanged "role declaration already '$DOCTOR_ROLE'"
    else
        printf '%s\n' "$DOCTOR_ROLE" > "$target"
        changed "declared role '$DOCTOR_ROLE' in $target (was '${current:-unset}')"
    fi

    # #2096 + "one question, one answer": confirm through #3128's resolver,
    # the ONLY thing entitled to read this file, rather than re-reading it.
    local py resolved
    if py="$(coord_py)"; then
        resolved="$("$py" - "$COORD_DIR" <<'PY' 2>/dev/null || true
import sys
from pathlib import Path
from coord.deploy_manifest import resolve_role
d = resolve_role(Path(sys.argv[1]))
print(f"{d.role}|{d.source}|{d.valid}")
PY
)"
        # The resolver is asked with the REAL environment, not a scrubbed one:
        # `COORD_ROLE` wins over the file (#3128), so an export left in a
        # profile would shadow what we just wrote — silently, and only on this
        # host. Either source answering with the right role is fine; a
        # disagreement is not.
        case "$resolved" in
            "$DOCTOR_ROLE|file|True"|"$DOCTOR_ROLE|env|True")
                unchanged "resolve_role() reads back '$DOCTOR_ROLE' (source: ${resolved#*|})" ;;
            "") warn "could not confirm the role through deploy_manifest.resolve_role" ;;
            *)  die "wrote role '$DOCTOR_ROLE' but #3128's resolver reads '$resolved' — the
declaration is not being picked up, and every role-aware check downstream
would silently grade this host as a worker." ;;
        esac
    fi
}

# ── Phase 6: THE credential block ────────────────────────────────────────────

# Read a secret without it ever reaching argv, a log line, or a non-0600 file.
# `read -rs` keeps it off the terminal; `printf` is a shell BUILTIN so the
# value never appears in /proc/*/cmdline; the file is created 0600 BEFORE it
# is written to, so there is no window where it is world-readable.
ask_secret() {
    local prompt="$1" dest="$2" value=""
    printf '    %s: ' "$prompt" >&2
    read -rs value || true
    printf '\n' >&2
    [[ -n "$value" ]] || return 1
    install -m 0600 /dev/null "$dest"
    printf '%s\n' "$value" > "$dest"
    value=""
    return 0
}

# Same, for a KEY=VALUE line appended to an env file.
ask_secret_kv() {
    local prompt="$1" key="$2" dest="$3" value=""
    printf '    %s: ' "$prompt" >&2
    read -rs value || true
    printf '\n' >&2
    [[ -n "$value" ]] || return 1
    [[ -f "$dest" ]] || install -m 0600 /dev/null "$dest"
    chmod 0600 "$dest"
    printf '%s=%s\n' "$key" "$value" >> "$dest"
    value=""
    return 0
}

phase_credentials() {
    log "credentials — EVERY prompt in this run happens here, now, in one block"
    cat <<EOF
    This is the only phase that needs you. Nothing after it prompts, so you can
    walk away once it is done. About to ask for, in order:

      1. tailscale   — a browser login to join the tailnet
      2. gh          — 'gh auth login' (forge read; on a server, merge rights too)
      3. git push    — an ssh key for pushing branches, generated here if absent
      4. ssh in      — the coordinator's public key, so 'machine doctor --ssh'
                       and every remote log/restart can reach this host
      5. claude      — the Claude Code OAuth login (Max/Pro subscription)
      6. board token — the daemon's bearer token (\$COORD_SERVE_TOKEN)
EOF
    [[ "$ROLE" == "server" ]] && cat <<EOF
      7. backup.env  — restic repository + password for the off-site DR lane.
                       If you keep these in Key Vault you can paste them from
                       there; nothing here REQUIRES Azure, and a machine must
                       stay buildable with a laptop and a browser.
EOF
    cat <<EOF

    Anything already configured is skipped — a re-run of this script does not
    re-prompt. No value you type is echoed, logged, or passed on a command
    line; each lands directly in a mode-0600 file.
EOF
    confirm_or_die

    # 1. tailscale
    if tailscale status >/dev/null 2>&1; then
        unchanged "tailscale is already up"
    else
        info "running 'sudo tailscale up' — follow the URL it prints"
        sudo tailscale up
        tailscale status >/dev/null 2>&1 \
            || die "'tailscale up' returned but 'tailscale status' still fails — not on the tailnet"
        changed "joined the tailnet"
    fi

    # 2. gh
    if gh auth status >/dev/null 2>&1; then
        unchanged "gh is authenticated"
    else
        gh auth login
        gh auth status >/dev/null 2>&1 \
            || die "'gh auth login' returned but 'gh auth status' still fails"
        changed "authenticated gh"
    fi

    # 3. git push key
    mkdir -p "$HOME/.ssh"; chmod 0700 "$HOME/.ssh"
    if [[ -f "$HOME/.ssh/id_ed25519" ]]; then
        chmod 0600 "$HOME/.ssh/id_ed25519"
        unchanged "ssh key present at ~/.ssh/id_ed25519"
    else
        ssh-keygen -t ed25519 -N '' -C "$MACHINE_NAME-coord" -f "$HOME/.ssh/id_ed25519"
        chmod 0600 "$HOME/.ssh/id_ed25519"
        changed "generated ~/.ssh/id_ed25519"
    fi
    if have gh && gh auth status >/dev/null 2>&1; then
        if gh ssh-key list 2>/dev/null | grep -qF "$(cut -d' ' -f2 < "$HOME/.ssh/id_ed25519.pub")"; then
            unchanged "this host's key is already on the forge account"
        else
            info "registering this host's public key with the forge so pushes work"
            gh ssh-key add "$HOME/.ssh/id_ed25519.pub" --title "$MACHINE_NAME (coord)" \
                || warn "could not add the key automatically — add it by hand; layer 8 will flag it"
            changed "offered ~/.ssh/id_ed25519.pub to the forge account"
        fi
    fi

    # 4. inbound ssh — what makes the doctor's --ssh probe (and layers 7-8)
    #    possible at all. Without it the gate reads UNKNOWN, never a pass.
    local authorized="$HOME/.ssh/authorized_keys" own_pub coordinator_key=""
    own_pub="$(cat "$HOME/.ssh/id_ed25519.pub")"
    [[ -f "$authorized" ]] || { install -m 0600 /dev/null "$authorized"; }
    chmod 0600 "$authorized"
    if grep -qF "$own_pub" "$authorized" 2>/dev/null; then
        unchanged "this host can ssh to itself (own key authorized)"
    else
        printf '%s\n' "$own_pub" >> "$authorized"
        changed "authorized this host's own key for the doctor's self-probe"
    fi
    if [[ "$ASSUME_YES" != "1" ]]; then
        printf "    paste the COORDINATOR's public key (or Enter to skip): "
        read -r coordinator_key || true
        if [[ -n "$coordinator_key" ]]; then
            if grep -qF "$coordinator_key" "$authorized" 2>/dev/null; then
                unchanged "the coordinator's key is already authorized"
            else
                printf '%s\n' "$coordinator_key" >> "$authorized"
                changed "authorized the coordinator's key"
            fi
        else
            warn "no coordinator key given — 'coord machine doctor $MACHINE_NAME --ssh',
      'coord log --machine' and remote restarts will not work from the
      coordinator until you add one (docs/AGENT_OPERATIONS.md, §Passwordless SSH)."
        fi
    fi

    # 5. Claude OAuth
    if [[ -f "$HOME/.claude/.credentials.json" ]]; then
        chmod 0600 "$HOME/.claude/.credentials.json" 2>/dev/null || true
        unchanged "Claude Code credentials present"
    else
        info "logging in to Claude Code — this uses the Max/Pro subscription, not an API key"
        claude /login || claude || true
        [[ -f "$HOME/.claude/.credentials.json" ]] \
            || die "the Claude login returned but ~/.claude/.credentials.json does not exist.
Every dispatched worker on this machine is 'claude -p'; without this the agent
accepts work and fails on the first task."
        chmod 0600 "$HOME/.claude/.credentials.json" 2>/dev/null || true
        changed "authenticated Claude Code"
    fi

    # 6. board bearer token (coord.serve_app.SERVE_TOKEN_FILE)
    local token_file="$COORD_DIR/serve_token"
    if [[ -s "$token_file" ]]; then
        chmod 0600 "$token_file"
        unchanged "board token present at $token_file"
    else
        if ask_secret "board bearer token (\$COORD_SERVE_TOKEN on the daemon)" "$token_file"; then
            changed "wrote the board token to $token_file (0600)"
        else
            warn "no board token given — this machine cannot talk to 'coord serve';
      layer 8's identity.board_token check will CRIT and the gate will fail."
        fi
    fi

    # 7. the DR credential, daemon only (machine_onboard's ROLE_IDENTITY_CHECKS
    #    holds only the daemon role to backup_env).
    if [[ "$ROLE" == "server" ]]; then
        local backup_env="$COORD_DIR/backup.env"
        if [[ -s "$backup_env" ]]; then
            chmod 0600 "$backup_env"
            unchanged "backup.env present at $backup_env"
        else
            info "off-site backup credentials (docs/AGENT_OPERATIONS.md §coord-backup)."
            info "these may come from Key Vault — paste them; Azure is never required."
            # No pre-created empty file: `ask_secret_kv` creates it 0600 only
            # when a value actually arrives, so an aborted prompt leaves no
            # zero-byte backup.env behind for the next run to mistake for one.
            ask_secret_kv "restic repository URL (COORD_BACKUP_REPOSITORY)" \
                COORD_BACKUP_REPOSITORY "$backup_env" \
                || warn "no restic repository given"
            ask_secret_kv "restic repository password (RESTIC_PASSWORD)" \
                RESTIC_PASSWORD "$backup_env" \
                || warn "no restic password given"
            if [[ -s "$backup_env" ]]; then
                changed "wrote $backup_env (0600)"
            else
                warn "$backup_env is empty — identity.backup_env will CRIT for a daemon."
            fi
        fi
        if have restic; then
            unchanged "restic $(restic version 2>/dev/null | head -1)"
        else
            apt_update_once
            sudo apt-get install -y -qq restic \
                || die "apt-get install restic failed — check apt sources/network and re-run
(this script is safe to re-run from the top)."
            have restic || die "restic install reported success but the binary is not on PATH.
A daemon without it fails toolchain.role_tool_missing and the DR lane is dead."
            changed "installed restic"
        fi
    fi

    # Belt and braces: nothing this phase wrote may be group/world readable.
    local f
    for f in "$COORD_DIR/serve_token" "$COORD_DIR/backup.env" \
             "$HOME/.ssh/id_ed25519" "$HOME/.claude/.credentials.json"; do
        [[ -f "$f" ]] || continue
        chmod 0600 "$f"
        [[ "$(stat -c '%a' "$f")" == "600" ]] \
            || die "$f is not mode 0600 after chmod — refusing to continue with a
readable credential on disk."
    done
}

# ── Phase 7: registration ────────────────────────────────────────────────────

phase_register() {
    local coord; coord="$(coord_bin)" || die "no coord binary — phase coord-cli did not run"
    local tracked="$SETTINGS_DIR/coord/coordinator.yml"

    if [[ -d "$SETTINGS_DIR/.git" ]]; then
        unchanged "settings checkout present at $SETTINGS_DIR"
    else
        mkdir -p "$(dirname "$SETTINGS_DIR")"
        git clone "https://github.com/${GITHUB_ORG}/coord-settings.git" "$SETTINGS_DIR"
        changed "cloned the settings checkout to $SETTINGS_DIR"
    fi
    [[ -f "$tracked" ]] || die "no tracked coordinator.yml at $tracked — the settings
checkout is not what this script expected. Fix it and re-run."

    # `host:` — prefer the MagicDNS FQDN, which a LAN DNS entry cannot shadow
    # (#2912). `coord machine add --verify-host` re-checks this and says so.
    if [[ -z "$HOST_NAME" ]]; then
        HOST_NAME="$(tailscale status --json 2>/dev/null \
            | jq -r '.Self.DNSName // empty' 2>/dev/null | sed 's/\.$//' || true)"
        [[ -n "$HOST_NAME" ]] || HOST_NAME="$MACHINE_NAME"
    fi

    # "Is this machine registered?" is a question `coord.config` already
    # answers; asking YAML with grep would be a second implementation that
    # agrees today and drifts later.
    local py registered="unknown"
    if py="$(coord_py)"; then
        registered="$("$py" - "$tracked" "$MACHINE_NAME" <<'PY' 2>/dev/null || true
import sys
from pathlib import Path
from coord.config import load
try:
    cfg = load(Path(sys.argv[1]))
except Exception as exc:  # a config that does not load is a finding, not a "no"
    print(f"error:{exc}")
else:
    print("yes" if any(m.name == sys.argv[2] for m in cfg.machines) else "no")
PY
)"
    fi
    case "$registered" in
        error:*) die "$tracked does not load: ${registered#error:}
Registering onto a config that is already broken would hide the real fault." ;;
        unknown|"") die "could not ask coord.config whether $MACHINE_NAME is registered
(the venv python is missing) — refusing to guess by grepping YAML." ;;
    esac

    local config_moved=0
    if [[ "$registered" == "yes" ]]; then
        unchanged "machines[$MACHINE_NAME] already in $tracked"
    else
        "$coord" machine add "$MACHINE_NAME" --host "$HOST_NAME" \
            --capabilities "$CAPABILITIES" --repos "$REPOS" --config "$tracked"
        changed "wrote machines[$MACHINE_NAME] to $tracked"
        config_moved=1
        warn "COMMIT AND PUSH $SETTINGS_DIR — the fleet runs the COMMITTED config.
      Nothing here commits for you; a local-only edit is invisible to every
      other machine, including the one running 'coord plan'."
    fi

    # The live config must be a SYMLINK into the checkout: if it is ever a
    # regular file, a correctly committed-and-pushed edit has no effect here
    # and nothing says so (#2915).
    local live="$COORD_DIR/coordinator.yml"
    if [[ -L "$live" ]] && [[ "$(readlink -f "$live")" == "$(readlink -f "$tracked")" ]]; then
        unchanged "$live -> $tracked"
    else
        if [[ -e "$live" && ! -L "$live" ]]; then
            mv -- "$live" "$live.replaced-by-provision.$(date +%s)"
            warn "$live was a regular FILE, not a symlink — moved aside (#2915)"
        fi
        ln -sfn "$tracked" "$live"
        changed "linked $live -> $tracked"
        config_moved=1
    fi

    # install-agent.sh started the agent before this config existed, so on a
    # first run it is up CONFIG-FREE and publishes no capabilities at all
    # (#1712 / #2915 incident 3). Restart it when the config moved — or when
    # it is not answering, whatever the reason.
    local health
    health="$(_agent_health 1)"
    if [[ $config_moved -eq 0 && -n "$health" ]]; then
        unchanged "coord-agent already answering /health on the current config"
    else
        systemctl --user restart coord-agent
        # An agent that "was restarted" is not evidence of an agent that is
        # answering: poll after the action (#2096).
        health="$(_agent_health 30)"
        [[ -n "$health" ]] || die "coord-agent did not answer /health on port $AGENT_PORT within 30s
after a restart. 'systemctl --user status coord-agent' and 'journalctl --user
-u coord-agent -n 50' have the reason."
        changed "coord-agent restarted onto $live and answered /health"
    fi
}

_agent_health() {
    local budget="${1:-1}" waited=0 body=""
    while :; do
        body="$(curl -fsS --max-time 3 "http://127.0.0.1:$AGENT_PORT/health" 2>/dev/null || true)"
        [[ -n "$body" ]] && { printf '%s' "$body"; return 0; }
        waited=$((waited + 1))
        [[ $waited -ge $budget ]] && return 0
        sleep 1
    done
}

# ── Phase 8: toolchains (worker, server) ─────────────────────────────────────

# `command -v chromium-browser` is NOT evidence of a browser on the one OS this
# script targets, and believing it is what #1678 looks like from the inside.
# Verified on a pristine Ubuntu 24.04.4 rootfs:
#
#   apt-cache policy chromium          ->  Candidate: (none)
#   apt-cache policy chromium-browser  ->  Candidate: 2:1snap1-0ubuntu2
#
# i.e. `chromium` does not exist as a deb in noble at all, and `chromium-browser`
# is a 50 KB TRANSITIONAL package whose entire /usr/bin/chromium-browser is
#
#   if ! [ -x /snap/bin/chromium ]; then
#       echo "Command '$0' requires the chromium snap to be installed." >&2
#       exit 1
#   fi
#
# with a postinst that installs no snap. So the historical
# `chromium-browser || chromium` pair plus a `have chromium-browser` check
# resolves, on noble, to "install a stub, find the stub on PATH, declare a
# browser" — apt exits 0, the check passes, and the machine advertises a
# capability it cannot honour. That silent false green is exactly the standing
# UNMET browser probe. The probe below therefore asks a candidate for its
# --version (which needs no display) instead of asking PATH whether a name
# exists, and tries the stub name LAST so the snap wrapper wins when both are
# present.
BROWSER_BIN=""
browser_works() {
    local bin
    BROWSER_BIN=""
    for bin in chromium google-chrome google-chrome-stable chromium-browser; do
        have "$bin" || continue
        "$bin" --version >/dev/null 2>&1 || continue
        BROWSER_BIN="$bin"
        return 0
    done
    return 1
}

phase_toolchains() {
    if [[ -z "$CAPABILITIES" ]]; then
        unchanged "no capabilities declared — nothing to install"
        return 0
    fi
    local cap
    for cap in ${CAPABILITIES//,/ }; do
        case "$cap" in
            rust)
                if have cargo && have rustc; then
                    unchanged "rust $(rustc --version 2>/dev/null)"
                else
                    # #1671: the agent unit's PATH includes ~/.cargo/bin, so a
                    # per-user rustup install IS visible to workers here —
                    # unlike the golden image, whose unit PATH predates that
                    # and therefore needs the core's system-wide
                    # $COORD_RUST_HOME variant. Same toolchain and profile,
                    # different destination *because the substrate differs*:
                    # both spellings live side by side in the core so the
                    # divergence is one file's decision, not two scripts'.
                    coord_core_install_rust_per_user \
                        || die "rustup reported success but cargo/rustc are not both resolvable.
Finding cargo without rustc is the exact false-green #1671 documents."
                    changed "installed rust $(rustc --version)"
                fi ;;
            gtk)
                if pkg-config --exists gtk4 2>/dev/null; then
                    unchanged "gtk4 $(pkg-config --modversion gtk4)"
                else
                    apt_update_once
                    sudo apt-get install -y -qq --no-install-recommends libgtk-4-dev \
                        || die "apt-get install libgtk-4-dev failed — check apt sources/network
and re-run (this script is safe to re-run from the top)."
                    pkg-config --exists gtk4 2>/dev/null \
                        || die "libgtk-4-dev installed but pkg-config cannot see gtk4"
                    changed "installed gtk4 $(pkg-config --modversion gtk4)"
                fi ;;
            browser)
                if browser_works; then
                    unchanged "browser: $BROWSER_BIN $("$BROWSER_BIN" --version 2>/dev/null | head -1)"
                else
                    # snap FIRST, and apt only as the non-noble fallback — see
                    # browser_works() for why apt cannot produce a working
                    # chromium on the one OS this script targets.
                    if have snap; then
                        sudo snap install chromium \
                            || die "snap install chromium failed. On Ubuntu 24.04 the snap is the
only packaging that yields a working chromium (see 'apt-cache policy chromium'
— no candidate). Check snapd is running and re-run."
                        # A snap lands in /snap/bin, which a non-login shell's
                        # PATH need not contain — so without this the probe
                        # below would fail an install that actually worked.
                        case ":$PATH:" in *":/snap/bin:"*) ;; *) PATH="/snap/bin:$PATH" ;; esac
                        export PATH
                    else
                        apt_update_once
                        sudo apt-get install -y -qq --no-install-recommends chromium \
                            || sudo apt-get install -y -qq --no-install-recommends chromium-browser \
                            || die "apt-get install of both chromium and chromium-browser failed,
and there is no 'snap' on this host to fall back to — check apt sources/network
and re-run (this script is safe to re-run from the top)."
                    fi
                    hash -r 2>/dev/null || true
                    browser_works \
                        || die "browser install reported success but no browser answers --version.
On Ubuntu 24.04 this is the expected outcome of the apt path alone: the
'chromium-browser' deb is a stub that exits 1 without the snap. The 'browser'
capability reading unmet is what makes webapp Test stages retry forever with no
board-visible reason (#1678)."
                    changed "installed a browser: $BROWSER_BIN $("$BROWSER_BIN" --version 2>/dev/null | head -1)"
                fi
                case "$(command -v "$BROWSER_BIN")" in
                    /snap/bin/*)
                        warn "the browser resolves to $(command -v "$BROWSER_BIN"), i.e. /snap/bin.
      coord-agent's unit PATH must contain /snap/bin or the agent will not see
      it and the 'browser' capability probe stays unmet (#1678) even though the
      browser works from your login shell. Layer 7 of 'coord machine doctor
      --ssh' is what tells you which of the two you have." ;;
                esac ;;
            *)
                warn "no toolchain rule for capability '$cap' — layer 7 will tell you
      whether the agent can actually see the tool it implies." ;;
        esac
    done
}

# ── Phase 9: repo clones + the graph (worker, server) ────────────────────────

phase_repos() {
    local coord; coord="$(coord_bin)" || die "no coord binary"
    mkdir -p "$SRC_DIR"
    local repo dir
    for repo in ${REPOS//,/ }; do
        dir="$SRC_DIR/$repo"
        if [[ -d "$dir" ]] && git -C "$dir" rev-parse --verify HEAD >/dev/null 2>&1; then
            unchanged "clone $dir"
        else
            if [[ -d "$dir" ]]; then
                # A clone killed mid-flight: a directory with no valid HEAD.
                # Moved aside, never deleted — CLAUDE.md is explicit that
                # ~/src/<repo> is the worker WORKTREE BASE and deleting one to
                # "fix drift" is how live worktrees get orphaned. Moving is
                # resumable AND non-destructive.
                local aside="$dir.incomplete.$(date +%s)"
                mv -- "$dir" "$aside"
                warn "$dir had no valid HEAD (an interrupted clone) — moved to $aside"
            fi
            git clone --filter=blob:none "https://github.com/${GITHUB_ORG}/${repo}.git" "$dir"
            git -C "$dir" rev-parse --verify HEAD >/dev/null 2>&1 \
                || die "git clone exited 0 but $dir has no HEAD"
            changed "cloned $dir"
        fi
    done

    # graphify + `core.hooksPath .githooks`, both of which fail SILENTLY when
    # absent (docs/GRAPHIFY_SETUP.md): every graph query on this machine
    # degrades to grep and nothing says so. `coord repo doctor --fix` is the
    # one implementation of that repair; this script does not grow a second.
    for repo in ${REPOS//,/ }; do
        "$coord" repo doctor "$repo" --fix || warn "coord repo doctor $repo --fix reported problems"
    done
    unchanged "ran 'coord repo doctor --fix' for: $REPOS"
}

# ── Phase 10: the daemon units (server) ──────────────────────────────────────

phase_daemon_units() {
    local py; py="$(coord_py)" || die "no venv python — phase coord-cli did not run"

    # WHICH units is not this script's opinion: `deploy_manifest.ROLE_UNITS` is
    # the authoritative per-role list (#2098), and `coord release verify` /
    # `deploy_units.install_units` both deliberately refuse to guess. So ask
    # the manifest, and install from the PACKAGED unit dir inside the
    # installed distribution — the released artifact, which cannot drift with
    # whatever this checkout happens to hold (#1927).
    # The one thing the released artifact demonstrably does NOT carry is the
    # `*.sh` helpers half the units ExecStart: `coord/deploy/README.md` says
    # so in as many words and tests/test_packaged_deploy_units.py pins it
    # (only `coord-db-backup.sh` is copied across). For those, the checkout
    # this script is being run FROM is the only source there is — see the
    # fallback in the plan below.
    local helper_fallback=""
    [[ -d "$REPO_ROOT/deploy" ]] && helper_fallback="$REPO_ROOT/deploy"

    local plan
    plan="$("$py" - "$SYSTEMD_USER_DIR" "$MACHINE_NAME" "$AGENT_PORT" "$LOCAL_BIN_DIR" \
            "$helper_fallback" <<'PY'
import sys
from pathlib import Path

from coord.deploy_manifest import ROLE_DAEMON, units_for_role
from coord.deploy_units import render_unit
from coord.health.checks.unit_drift import packaged_unit_dir

dest = Path(sys.argv[1]); machine, port = sys.argv[2], sys.argv[3]
helper_dest = Path(sys.argv[4])
fallback = Path(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else None
ref = packaged_unit_dir()
if ref is None:
    print("ERROR|this install ships no coord/deploy/ — upgrade the Python lane first")
    raise SystemExit(0)
dest.mkdir(parents=True, exist_ok=True)

manifest = list(units_for_role(ROLE_DAEMON))

# ROLE_UNITS names what should be ENABLED, which for a timer is the timer.
# The unit a timer actually runs is the .service of the same stem, and
# `Unit=` defaults to it implicitly — so it is never named in ROLE_UNITS and
# would not be installed here. An enabled timer whose .service file is
# missing is the worst possible shape: `is-enabled` says `enabled`, the timer
# arms, and every single fire dies with "Unit coord-db-backup.service not
# found" in a journal nobody reads. Install those companions too (they are
# pulled in by the timer, so they are deliberately NOT separately enabled).
companions = []
for unit in manifest:
    if unit.endswith(".timer"):
        companion = unit[: -len(".timer")] + ".service"
        if companion not in manifest and (ref / companion).exists():
            companions.append(companion)

for unit in manifest + companions:
    if unit == "coord-agent.service":
        continue  # install-agent.sh owns it, and already installed it.
    suffix = "-DEP" if unit in companions else ""
    src = ref / unit
    if not src.exists():
        print(f"ERROR|{unit} is in ROLE_UNITS but not packaged in {ref}")
        continue
    text, note = render_unit(src.read_text(encoding="utf-8"), machine_name=machine, port=port)
    if text is None:
        print(f"ERROR|{unit}: {note}")
        continue
    target = dest / unit
    if target.is_symlink() and str(target.readlink()) == "/dev/null":
        print(f"MASKED|{unit}")   # an operator's explicit "never run this" (#2812)
        continue
    if target.exists() and target.read_text(encoding="utf-8") == text:
        print(f"SAME{suffix}|{unit}")
        continue
    action = "UPDATED" if target.exists() else "NEW"
    tmp = target.with_name(target.name + ".coord-tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    print(f"{action}{suffix}|{unit}")

# The helper scripts the units above ExecStart, shipped alongside them —
# into ~/.local/bin, NOT the unit dir. `coord-db-backup.service` says
# `ExecStart=%h/.local/bin/coord-db-backup.sh`, and %h is the unit owner's
# HOME; a helper dropped next to the .service file is not on that path, so
# the unit installs and enables cleanly and then dies on every fire with
# status=203/EXEC. That is invisible from `is-enabled` — which is the whole
# #2082 failure shape — and on a daemon host it means the hourly coord.db
# snapshot silently never runs. Caught by tier 3 of
# scripts/verify-provision-noble.sh, which resolves every rendered ExecStart.
helper_dest.mkdir(parents=True, exist_ok=True)


def stage_helper(src: Path, note: str) -> None:
    """Copy one helper into place, idempotently, reporting what it did."""
    target = helper_dest / src.name
    text = src.read_text(encoding="utf-8")
    if target.exists() and target.read_text(encoding="utf-8") == text:
        print(f"HELPERSAME|{src.name}{note}")
        return
    action = "HELPERUPDATED" if target.exists() else "HELPERNEW"
    target.write_text(text, encoding="utf-8")
    target.chmod(0o755)
    print(f"{action}|{src.name}{note}")


packaged_helpers = set()
for helper in sorted(ref.glob("*.sh")):
    packaged_helpers.add(helper.name)
    stage_helper(helper, "")

# #2096, applied to systemd: a unit that installs and enables cleanly is not
# a unit that runs. Resolve every rendered ExecStart NOW, while there is an
# operator watching, instead of letting it surface as 203/EXEC in a journal
# nobody reads. `%h` is systemd's specifier for the unit owner's home.
home = str(Path.home())
for unit_path in sorted(dest.glob("*.service")):
    for line in unit_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("ExecStart="):
            continue
        parts = line[len("ExecStart="):].lstrip("-+!:@").split()
        if not parts:
            continue
        binary = Path(parts[0].replace("%h", home))
        if binary.exists():
            continue
        # LAST RESORT, and only for a helper this release genuinely does not
        # ship (`coord-web-dist-build.sh`, `coord-failure-notify.sh`): take it
        # from the checkout this script is running out of. A packaged helper is
        # NEVER overridden this way — the release stays authoritative for
        # everything it actually carries, so this cannot reintroduce the #1927
        # "installed from whatever the checkout happened to hold" drift. It is
        # strictly better than the alternative, which is arming a unit that can
        # only ever 203/EXEC.
        src = fallback / binary.name if fallback is not None else None
        if (
            src is not None
            and binary.name.endswith(".sh")
            and binary.name not in packaged_helpers
            and binary.parent.resolve() == helper_dest.resolve()
            and src.is_file()
        ):
            stage_helper(src, f" (from {fallback}; this coord release does not ship it)")
            continue
        print(f"DEADEXEC|{unit_path.name} runs {parts[0]}, which does not exist")
PY
)"

    local line action unit wanted=() errors=0
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        action="${line%%|*}"; unit="${line#*|}"
        case "$action" in
            ERROR)   warn "unit install: $unit"; errors=$((errors + 1)) ;;
            NEW)     changed "installed $unit"; wanted+=("$unit") ;;
            UPDATED) changed "refreshed $unit"; wanted+=("$unit") ;;
            SAME)    unchanged "$unit"; wanted+=("$unit") ;;
            # The *.sh helpers the units ExecStart. Never enabled — they are
            # not units — so they are deliberately not in `wanted`.
            HELPERNEW)     changed "staged helper $unit" ;;
            HELPERUPDATED) changed "refreshed helper $unit" ;;
            HELPERSAME)    unchanged "helper $unit" ;;
            # A timer's companion .service: installed so the timer has
            # something to run, never separately enabled (the timer pulls it).
            NEW-DEP)     changed "installed $unit (fired by its timer)" ;;
            UPDATED-DEP) changed "refreshed $unit (fired by its timer)" ;;
            SAME-DEP)    unchanged "$unit (fired by its timer)" ;;
            MASKED)  warn "$unit is masked by an operator — left masked (#2812)" ;;
            DEADEXEC)
                DEAD_EXEC=$((DEAD_EXEC + 1))
                warn "DEAD ExecStart: $unit.
      systemd will enable and arm it happily and then fail every start with
      203/EXEC, which 'is-enabled' cannot see, and which 'coord machine
      doctor' cannot see either — so THIS WARNING, and the dead_exec= count
      in the final PROVISION: line, are the only report you will get. It is
      deliberately advisory and not fatal: the remaining cases are missing
      release artifacts, and refusing to finish --role server over one would
      block a rebuild on something the operator cannot fix from this host.
      The file was not in this coord release's packaged coord/deploy/ AND not
      in ${helper_fallback:-<no deploy/ dir beside this script>}, the checkout
      this script is running from. Put it in $LOCAL_BIN_DIR by hand, or
      'systemctl --user mask' the unit named above so it stops being an armed
      unit that can only fail."
                ;;
        esac
    done <<< "$plan"
    [[ $errors -eq 0 ]] || die "$errors unit(s) could not be installed — see above."
    [[ ${#wanted[@]} -gt 0 ]] || die "the daemon manifest produced no installable units;
that is a broken install, not a clean host."

    systemctl --user daemon-reload
    local state
    for unit in "${wanted[@]}"; do
        state="$(systemctl --user is-enabled "$unit" 2>/dev/null || true)"
        if [[ "$state" == "enabled" ]]; then
            unchanged "$unit enabled"
        else
            systemctl --user enable --now "$unit"
            # #2096: `enable` exiting 0 is not the verdict — ASK systemd again.
            state="$(systemctl --user is-enabled "$unit" 2>/dev/null || true)"
            [[ "$state" == "enabled" ]] \
                || die "'systemctl --user enable --now $unit' returned but is-enabled
still reads '${state:-<nothing>}'. An installed-but-disabled unit's file looks
byte-for-byte identical to an active one's — that is exactly how the release
propagate timer sat dead on three hosts (#2082)."
            changed "enabled $unit"
        fi
    done

    loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
}

# ── Phase 11: the store (server) ─────────────────────────────────────────────

phase_store() {
    if mountpoint -q "$BACKUP_MOUNT" 2>/dev/null; then
        unchanged "backup volume mounted at $BACKUP_MOUNT"
    else
        warn "$BACKUP_MOUNT is not a mount point. The local db-backup timer in the
      daemon manifest snapshots coord.db onto the external SSD there; without
      it that lane writes to the root filesystem, which defeats the point of
      the lane. Mount it (an fstab entry with nofail) and re-run — this script
      deliberately does not edit fstab or format anything."
    fi

    cat <<EOF

    This host is now a daemon HOST. It is not yet the daemon's DATA.
    Restoring coord.db is docs/DISASTER_RECOVERY.md (landing via #3117) + #3129
    ('coord dr promote'), deliberately not duplicated here:

      coord dr status                 # is there a verified off-site snapshot?
      coord dr promote --help         # restore onto this host and serve /board

    Until you do that, 'coord serve' here is serving an EMPTY board — which
    looks identical to a healthy quiet fleet.
EOF
}

# ── Phase 12: the gate ───────────────────────────────────────────────────────

phase_gate() {
    local coord; coord="$(coord_bin)" || die "no coord binary"

    # The doctor's --ssh probe is what produces layers 7 (login-shell PATH) and
    # 8 (identity) at all; without it they read UNKNOWN, which is "never a
    # pass" but is also never a CRIT — so a gate run without it would exit 0
    # having graded nothing. Prove the probe can connect BEFORE trusting its
    # verdict.
    local host="${HOST_NAME:-$MACHINE_NAME}"
    ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
        "$host" true >/dev/null 2>&1 \
        || die "cannot ssh to '$host' from this machine, so 'coord machine doctor --ssh'
cannot run — and WITHOUT it the toolchain and identity layers report UNKNOWN
rather than failing. A gate that cannot see two of its eight layers is not a
gate, so this exits non-zero instead of reporting a green it did not earn.
Fix ~/.ssh/authorized_keys (the credentials phase offers to) and re-run."

    local out="" status=0
    set +e
    out="$("$coord" machine doctor "$MACHINE_NAME" --ssh -v --role "$DOCTOR_ROLE" 2>&1)"
    status=$?
    set -e
    printf '%s\n' "$out"

    # #2096: exit 0 alone is not evidence — a doctor that crashed before
    # producing a report, or a `coord` too old to know --role, could exit 0
    # having graded nothing. The verdict is the report's own machine-readable
    # trailer, and its ABSENCE is a failure, not a pass.
    local trailer
    trailer="$(printf '%s\n' "$out" | grep -E '^MACHINE_DOCTOR: ' | tail -1 || true)"
    [[ -n "$trailer" ]] || die "coord machine doctor produced no MACHINE_DOCTOR: trailer
(exit $status). A missing verdict is not a passing verdict."

    if [[ "$trailer" != *"ok=true"* || $status -ne 0 ]]; then
        printf '\n'
        printf 'GATE FAILED — failing layer(s): %s\n' \
            "$(printf '%s\n' "$out" | grep -F '✗ CRIT' \
                 | sed -nE 's/.*\[([a-z_]+)\.[a-z_]+\].*/\1/p' | sort -u | paste -sd, -)"
        printf '%s\n' "$out" | grep -F '✗ CRIT' | sed 's/^/  /'
        die "coord machine doctor is not clean for role '$DOCTOR_ROLE' ($trailer).

Fix the layers above and RE-RUN THIS WHOLE SCRIPT — every phase is idempotent,
so re-running is cheaper and safer than reconstructing where it stopped."
    fi
    unchanged "gate: $trailer"
}

# ── Run ──────────────────────────────────────────────────────────────────────

printf 'provision-machine: role=%s machine=%s doctor-role=%s\n' \
    "$ROLE" "$MACHINE_NAME" "$DOCTOR_ROLE"

# Materialised into an array FIRST, deliberately: iterating `while read` over
# a process substitution rebinds stdin for the whole loop body, so the
# credential phase's own `read` would consume phase names instead of what the
# operator typed — silently, and only for the interactive path.
mapfile -t PHASES_TO_RUN < <(phase_names_for_role "$ROLE")

PHASES_RUN=0
for name in "${PHASES_TO_RUN[@]}"; do
    CURRENT_PHASE="$name"
    PHASES_RUN=$((PHASES_RUN + 1))
    log "phase $PHASES_RUN: $name ($(phase_speed "$name"))"
    "phase_${name//-/_}"
done
CURRENT_PHASE=""

printf '\n'
printf 'PROVISION: role=%s machine=%s phases=%d changes=%d dead_exec=%d gate=pass\n' \
    "$ROLE" "$MACHINE_NAME" "$PHASES_RUN" "$CHANGES" "$DEAD_EXEC"
if [[ $CHANGES -eq 0 ]]; then
    printf 'Nothing changed — this machine was already provisioned for role %s.\n' "$ROLE"
fi
if [[ $DEAD_EXEC -gt 0 ]]; then
    # The gate above is green and this is still true: `coord machine doctor`
    # grades units by `is-enabled`, which reads `enabled` for a unit that
    # 203/EXECs on every fire. So say it once more, after the trailer, where
    # an operator who scrolled past the phase output still sees it.
    printf '%d unit(s) are enabled with an ExecStart that resolves to nothing — they\n' \
        "$DEAD_EXEC"
    printf 'will fail 203/EXEC on every fire and the doctor cannot see it. Search this\n'
    printf "run's output for 'DEAD ExecStart' for the unit names and what to do.\n"
fi
