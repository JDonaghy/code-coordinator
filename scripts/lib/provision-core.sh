#!/usr/bin/env bash
# shellcheck shell=bash
#
# ONE shared provisioning core for both fleet-machine lanes (#3139).
#
#   scripts/azure-workers/provision-worker.sh   the Azure golden IMAGE lane
#   scripts/provision-machine.sh                the bare-metal lane (#3138)
#
# WHAT LIVES HERE, AND WHY IT IS NOT A STYLE PREFERENCE
# ----------------------------------------------------
# This file owns *what a fleet machine needs*. Each wrapper owns *the
# constraints of its substrate*. The split is not cosmetic: every value below
# has already drifted once, or is one copy away from drifting, and each drift
# was paid for:
#
#   COORD_GH_MIN_VERSION   Ubuntu's packaged `gh` is far below it; an image
#                          built with it fails the CI merge gate with
#                          `coord.github_ops.GhTooOldForJsonChecks` — at merge
#                          time, not at build time. Pinned to the Python
#                          constant by tests/test_provision_core.py, which
#                          fails when the two disagree.
#   COORD_OPENCODE_VERSION A skew between the image and the standing fleet was
#                          a real failure (#1777).
#   COORD_RUST_HOME        A per-user rustup install left `cargo` invisible to
#                          every dispatched task (#1671) on any host whose
#                          agent unit PATH lacks ~/.cargo/bin — which is the
#                          image lane's.
#   COORD_NODE_MAJOR       Two lanes silently baking different node majors is
#                          a class of bug nobody looks for.
#
# The failure mode is asymmetric and quiet: a drifted image is discovered ~30
# minutes after a build reports success, at deploy time, on a VM you are
# paying for. So the rule this file enforces is that each of those values
# exists EXACTLY ONCE across both lanes — greppable, and grepped, by
# tests/test_provision_core.py.
#
# WHAT DELIBERATELY STAYS IN THE WRAPPERS
# ---------------------------------------
#                    | Azure image                | bare metal
#   user             | creates a dedicated `coord`| the operator's own account
#                    | user (waagent deletes the  |
#                    | provisioning user + home)  |
#   identity         | NONE by design             | the whole point
#   tailscale        | installed, never `up`      | `tailscale up`, interactive
#   privilege        | root throughout            | sudo for packages only
#   finish           | scrub-and-generalize.sh    | `coord machine doctor`
#
# Nothing in this file may assume root, a particular user, a home directory,
# or that identity exists. Where a step needs privilege it goes through
# $COORD_CORE_SUDO (empty when already root); where it needs to run as a
# specific user, the WRAPPER runs it, and this file only supplies the command
# — see coord_core_opencode_install_cmd.
#
# SOURCING
# --------
#   . "$(dirname "$0")/../lib/provision-core.sh"
# The image lane is `scp`'d to a builder VM by build-worker-image.sh, which
# copies this file alongside it (into ./lib/ next to the script); both layouts
# are handled by coord_core_locate below, so neither lane hardcodes a path.

# Guard against double-sourcing: both lanes may source this more than once
# (e.g. a wrapper that re-execs), and re-declaring readonly values would abort
# under `set -e`.
[[ -n "${COORD_PROVISION_CORE_LOADED:-}" ]] && return 0
COORD_PROVISION_CORE_LOADED=1

# ── The pinned values. Exactly one copy each, fleet-wide. ────────────────────

# coord.github_ops.GH_PR_CHECKS_JSON_MIN_VERSION. This is a REAL link, not a
# comment: tests/test_provision_core.py imports the Python constant and fails
# when it and this line disagree. Do not restate it in either wrapper.
COORD_GH_MIN_VERSION="2.86.0"

# The node major both lanes install. The image lane takes it from nodesource;
# the bare-metal lane takes it from nvm (install-agent.sh's node shims resolve
# an nvm install at RUN time — #1678 — so a root nodesource install there is
# not re-resolvable). Different installers, same major: that is the whole
# point of the value living here.
COORD_NODE_MAJOR="22"

# Pinned to match the standing fleet — see #1777. A version skew between the
# image and the fleet was a real failure.
COORD_OPENCODE_VERSION="1.18.11"

# #1671's root cause. install-agent.sh pins the agent unit's PATH to
#   $VENV/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin
# — note the absence of ~/.cargo/bin. Workers inherit that PATH, so on the
# image lane a per-user rustup install leaves `cargo` invisible to every
# dispatched task. System-wide + symlinked into /usr/local/bin is the fix.
COORD_RUST_HOME="/opt/rust"

# The Python floor install-agent.sh enforces, as an ERE for `grep -qE`.
COORD_PYTHON_MIN_VERSION="3.12"
COORD_PYTHON_MIN_RE='3\.(1[2-9]|[2-9][0-9])'

# The repos a fleet machine clones, and the forge account they live under.
# `~/src/<repo>` is the worker WORKTREE BASE — `git worktree add` runs from it
# (CLAUDE.md). #2899: coord-tui is its own repo, named by `coordinator.yml`'s
# `repo_paths.coord-tui` on every machine that carries it.
COORD_GITHUB_ORG="${COORD_GITHUB_ORG:-JDonaghy}"
COORD_FLEET_REPOS="code-coordinator coord-tui quadraui vimcode"

# probe|apt-package. The PROBE is what the fleet actually needs to WORK, not
# what dpkg happens to have recorded — `python3 -m ensurepip` in particular is
# the #2911 trap in its original form: Ubuntu 24.04 ships a python3 whose venv
# module cannot bootstrap pip, and install-agent.sh then dies naming pip
# rather than the missing package.
#
# The bare-metal lane probes each entry and installs only what is missing; the
# image lane installs the package column outright (a builder VM is empty by
# definition, so probing it first would only cost a round trip). Both read
# THIS list.
COORD_BASE_REQUIREMENTS=(
    "git|git"
    "curl|curl"
    "jq|jq"
    "tmux|tmux"
    "rsync|rsync"
    "unzip|unzip"
    "gcc|build-essential"
    "pkg-config|pkg-config"
    "rg|ripgrep"
    "python3|python3"
    "python3 -m venv --help|python3-venv"
    "python3 -m ensurepip --version|python3-venv"
    "ssh|openssh-client"
    "ssh-keygen|openssh-client"
)

# The prereq verification both lanes owe, mirroring coord/prereqs.py's
# BASELINE_PREREQS. tests/test_provision_core.py cross-checks this against the
# real Python manifest, so a prereq added there cannot silently stop being
# verified at provisioning time.
#   name|command to run for a version line
COORD_PREREQ_CHECKS=(
    "git|git --version"
    "gh|gh --version"
    "cargo|cargo --version"
    "python3|python3 --version"
    "tmux|tmux -V"
    "node|node --version"
)

# Privilege prefix. Root (the image builder) needs none; the bare-metal lane
# runs as the operator and uses sudo for packages only. A wrapper may set this
# before sourcing; otherwise it is inferred.
if [[ -z "${COORD_CORE_SUDO+x}" ]]; then
    if [[ "$(id -u)" -eq 0 ]]; then COORD_CORE_SUDO=""; else COORD_CORE_SUDO="sudo"; fi
fi

# ── Locating this file from a wrapper ───────────────────────────────────────

# Print the path to this library, given the directory of the calling script.
# Two layouts are supported and neither wrapper hardcodes one:
#   <repo>/scripts/azure-workers/provision-worker.sh -> ../lib/provision-core.sh
#   /tmp/provision-worker.sh (scp'd to a builder)    -> ./lib/provision-core.sh
# $COORD_PROVISION_CORE overrides both, for tests and for odd deployments.
coord_core_locate() {
    local here="${1:?coord_core_locate <script-dir>}" cand
    if [[ -n "${COORD_PROVISION_CORE:-}" ]]; then
        printf '%s\n' "$COORD_PROVISION_CORE"; return 0
    fi
    for cand in "$here/../lib/provision-core.sh" "$here/lib/provision-core.sh"; do
        [[ -f "$cand" ]] && { printf '%s\n' "$cand"; return 0; }
    done
    return 1
}

# ── Small shared predicates ─────────────────────────────────────────────────

coord_core_have() { command -v "$1" >/dev/null 2>&1; }

# `coord_core_version_meets_floor <floor> <version>` — true when version >= floor.
# One implementation of the comparison, so the two lanes cannot disagree about
# what "meets the floor" means (they each had their own `sort -V` incantation).
# An EMPTY or unparseable version is NOT a pass: a probe that produced nothing
# is a failure, never a permissive default.
coord_core_version_meets_floor() {
    local floor="${1:-}" version="${2:-}"
    [[ -n "$floor" ]]   || return 1
    [[ -n "$version" ]] || return 1
    [[ "$version" =~ ^[0-9] ]] || return 1
    [[ "$(printf '%s\n%s\n' "$floor" "$version" | sort -V | head -1)" == "$floor" ]]
}

# The apt package column of COORD_BASE_REQUIREMENTS, deduplicated, one per line.
coord_core_base_packages() {
    local entry pkg seen=""
    for entry in "${COORD_BASE_REQUIREMENTS[@]}"; do
        pkg="${entry##*|}"
        case " $seen " in *" $pkg "*) continue ;; esac
        seen="$seen $pkg"
        printf '%s\n' "$pkg"
    done
}

# The fleet repo list as a comma-separated string (the form
# `coord machine add --repos` and provision-machine.sh's --repos take).
coord_core_repos_csv() { printf '%s\n' "${COORD_FLEET_REPOS// /,}"; }

# ── Python ──────────────────────────────────────────────────────────────────

# True when the given interpreter (default python3) clears the floor
# install-agent.sh enforces. Absence is a failure, not a skip.
coord_core_python_meets_floor() {
    local py="${1:-python3}"
    coord_core_have "$py" || return 1
    "$py" --version 2>&1 | grep -qE "$COORD_PYTHON_MIN_RE"
}

# ── gh ──────────────────────────────────────────────────────────────────────

# The installed gh's version, or nothing.
coord_core_gh_version() {
    coord_core_have gh || return 0
    gh --version 2>/dev/null | sed -n 's/^gh version \([0-9.]*\).*/\1/p' | head -1
}

# True when the installed gh clears COORD_GH_MIN_VERSION. No gh, or a gh whose
# --version says nothing, is FALSE — the floor check must be reachable in the
# failing direction, not defaulted past.
coord_core_gh_meets_floor() {
    coord_core_version_meets_floor "$COORD_GH_MIN_VERSION" "$(coord_core_gh_version)"
}

# Register the official GitHub CLI apt repository. Ubuntu's own `gh` is far
# below the floor, so this is not an optimisation — it is the only way to get
# a usable gh on either lane.
coord_core_add_gh_apt_source() {
    $COORD_CORE_SUDO install -d -m 0755 /usr/share/keyrings
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | $COORD_CORE_SUDO dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg status=none
    $COORD_CORE_SUDO chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
    printf 'deb [arch=%s signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' \
        "$(dpkg --print-architecture)" \
        | $COORD_CORE_SUDO tee /etc/apt/sources.list.d/github-cli.list >/dev/null
}

# Install gh from the official repo. Returns non-zero if apt fails; the FLOOR
# is a separate call so each lane can word its own hard-fail (both do fail).
coord_core_install_gh() {
    coord_core_add_gh_apt_source || return 1
    # A source list just changed, so any earlier `apt-get update` in this run
    # does not cover the new repo's index — this one is unconditional.
    $COORD_CORE_SUDO apt-get update -qq || return 1
    DEBIAN_FRONTEND=noninteractive $COORD_CORE_SUDO apt-get install -y -qq gh || return 1
}

# ── rust ────────────────────────────────────────────────────────────────────

# System-wide rust at $COORD_RUST_HOME, symlinked into /usr/local/bin. This is
# #1671's fix and the image lane's ONLY correct shape: the agent unit's PATH
# has no ~/.cargo/bin, so a per-user install is invisible to every worker.
# Requires root (the image builder is root throughout).
coord_core_install_rust_system_wide() {
    local bin
    # Exported for the REST of the caller's run, not just this function: the
    # /usr/local/bin/cargo symlinks are rustup proxies, which resolve their
    # toolchain through $RUSTUP_HOME. Without the export a non-login shell in
    # the caller (the image lane's own `cargo --version`) would look in
    # ~/.cargo and find no default toolchain. /etc/profile.d/rust.sh below
    # covers LOGIN shells; this covers the provisioning run itself.
    export RUSTUP_HOME="$COORD_RUST_HOME" CARGO_HOME="$COORD_RUST_HOME"
    curl -fsSL https://sh.rustup.rs \
        | sh -s -- -y --no-modify-path --profile minimal --default-toolchain stable \
        || return 1
    for bin in "$COORD_RUST_HOME"/bin/*; do
        ln -sf "$bin" "/usr/local/bin/$(basename "$bin")"
    done
    chmod -R a+rX "$COORD_RUST_HOME"
    cat > /etc/profile.d/rust.sh <<EOF
export RUSTUP_HOME=$COORD_RUST_HOME
export CARGO_HOME=\$HOME/.cargo
EOF
    # #2096: confirm AFTER the action, through the PATH a worker would use,
    # rather than trusting rustup's exit code. Finding cargo without rustc is
    # the exact false green #1671 documents.
    /usr/local/bin/cargo --version >/dev/null 2>&1 || return 1
    /usr/local/bin/rustc --version >/dev/null 2>&1 || return 1
}

# Per-user rustup, for a lane whose agent unit PATH DOES include ~/.cargo/bin
# (the bare-metal one — see provision-machine.sh's `rust` case for why that
# differs from the image lane). Same toolchain, same profile, different
# destination because the substrate differs.
coord_core_install_rust_per_user() {
    curl -fsSL https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal || return 1
    export PATH="$HOME/.cargo/bin:$PATH"
    coord_core_have cargo && coord_core_have rustc
}

# ── node ────────────────────────────────────────────────────────────────────

# nodesource, system-wide, at the pinned major. Root only (image lane).
coord_core_install_node_system_wide() {
    # `$COORD_CORE_SUDO -E bash -` would expand to a bare `-E bash -` when the
    # prefix is empty (already root), i.e. "run the command named -E" — so the
    # two privilege cases are spelled out rather than interpolated.
    if [[ -n "$COORD_CORE_SUDO" ]]; then
        curl -fsSL "https://deb.nodesource.com/setup_${COORD_NODE_MAJOR}.x" \
            | $COORD_CORE_SUDO -E bash - || return 1
    else
        curl -fsSL "https://deb.nodesource.com/setup_${COORD_NODE_MAJOR}.x" | bash - || return 1
    fi
    DEBIAN_FRONTEND=noninteractive $COORD_CORE_SUDO apt-get install -y -qq nodejs || return 1
    coord_core_have node
}

# ── opencode ────────────────────────────────────────────────────────────────

# The install command for the pinned opencode, as a STRING, because the two
# lanes run it as different users (the image lane via `sudo -u coord -H bash
# -lc`, which is substrate, not policy). The pin lives here; the identity of
# the user running it stays in the wrapper.
#
# The official installer always drops the binary at ~/.opencode/bin — there is
# no env var or flag that redirects it (verified against install.sh source,
# 2026-08-03: `INSTALL_DIR=$HOME/.opencode/bin` is unconditional, --binary only
# changes the SOURCE). So the second half symlinks it into ~/.local/bin, which
# the coord-agent unit's PATH already includes (deploy/coord-agent.service).
# That is what makes it land on the *agent process's* PATH without a
# coord-agent.service.d drop-in — the standing fleet needed one
# (20-opencode-path.conf) precisely because nothing else put ~/.opencode/bin
# on that PATH.
#
# Never authenticate here: no `opencode auth login`, no auth.json. A golden
# image must contain zero identity; the credential arrives at boot from Key
# Vault (bootstrap-shared.sh / docs/OPENCODE_VERIFICATION.md).
coord_core_opencode_install_cmd() {
    printf 'curl -fsSL https://opencode.ai/install | bash -s -- --version %s --no-modify-path\n' \
        "$COORD_OPENCODE_VERSION"
}

coord_core_opencode_link_cmd() {
    printf 'mkdir -p ~/.local/bin && ln -sf ~/.opencode/bin/opencode ~/.local/bin/opencode\n'
}

# True when the given version string is exactly the pinned one. Exact, not a
# floor: #1777 was a SKEW, in either direction.
coord_core_opencode_version_matches() {
    [[ -n "${1:-}" ]] && [[ "$1" == "$COORD_OPENCODE_VERSION" ]]
}

# ── prereq verification (mirrors coord/prereqs.py) ──────────────────────────
#
# Both lanes owe a verification pass over COORD_PREREQ_CHECKS. It prints one
# line per tool and returns non-zero if ANY is missing, so a caller cannot
# accidentally treat "nothing crashed" as "everything is present".

coord_core_check_tool() {
    local name="$1"; shift
    if coord_core_have "$1"; then
        printf '  %-10s %s\n' "$name" "$("$@" 2>&1 | head -1)"
        return 0
    fi
    printf '  %-10s MISSING\n' "$name"
    return 1
}

# Run every baseline prereq check. Returns the number of failures (capped by
# the shell's exit-status range), 0 when all present.
coord_core_verify_baseline_prereqs() {
    local entry name cmd fail=0
    for entry in "${COORD_PREREQ_CHECKS[@]}"; do
        name="${entry%%|*}"; cmd="${entry#*|}"
        # shellcheck disable=SC2086
        coord_core_check_tool "$name" $cmd || fail=$((fail + 1))
    done
    return "$fail"
}
