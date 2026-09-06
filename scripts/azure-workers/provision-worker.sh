#!/usr/bin/env bash
# Provision a coord worker golden image (Ubuntu 24.04 LTS, x86_64).
#
# Runs ON the builder VM. Installs every prereq + warms the caches that
# otherwise dominate an ephemeral worker's first task. Does NOT join the
# tailnet, does NOT authenticate anything, does NOT start the agent — a
# golden image must contain zero identity. See scrub-and-generalize.sh.
#
#   sudo ./provision-worker.sh [--with-gtk] [--with-browser] [--seed-cargo-target]
#
# Why a dedicated `coord` user rather than `azureuser`:
#   `waagent -deprovision+user` (scrub step) deletes the *provisioning* user
#   and its home directory. Everything expensive we bake -- ~/.coord-venv,
#   ~/src clones, ~/.npm -- lives in the home dir, so building as azureuser
#   means the scrub silently throws the entire image away. Building as a
#   separately-created user leaves /home/coord untouched by the deprovision.
set -euo pipefail

# The shared provisioning core (#3139). Every value this script used to
# restate -- the gh floor, the node major, the opencode pin, the rust
# location, the base package list, the repo clone list -- now lives THERE, in
# exactly one place across both lanes, and tests/test_provision_core.py fails
# the build if a second copy reappears here. What stays below is what is
# genuinely specific to a golden IMAGE: root throughout, a dedicated `coord`
# user, and zero identity.
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
COORD_CORE_SUDO=""            # this script is root throughout (checked below)
_core="$(
    if [[ -n "${COORD_PROVISION_CORE:-}" ]]; then printf '%s\n' "$COORD_PROVISION_CORE"
    elif [[ -f "$HERE/../lib/provision-core.sh" ]]; then printf '%s\n' "$HERE/../lib/provision-core.sh"
    elif [[ -f "$HERE/lib/provision-core.sh" ]]; then printf '%s\n' "$HERE/lib/provision-core.sh"
    fi
)"
# build-worker-image.sh scp's this script to a throwaway builder; it copies the
# core alongside it (into ./lib/ next to the script). A missing core is fatal,
# never a fallback to inlined values -- an image built from half a toolchain
# list is exactly the silent drift this file stopped restating in order to
# avoid.
[[ -n "$_core" && -f "$_core" ]] || {
    echo "cannot find lib/provision-core.sh (looked beside and above $HERE)." >&2
    echo "It must be copied to the builder alongside this script." >&2
    exit 1
}
# shellcheck source=../lib/provision-core.sh
. "$_core"

COORD_USER="${COORD_USER:-coord}"
CARGO_TARGET_SEED="/opt/cargo-target-seed"
# #2899: coord-tui joins the clone list as its own repo. `~/src/coord-tui` is
# what `coordinator.yml`'s `repo_paths.coord-tui` names on every machine that
# carries it, and what `resolve_coord_tui_checkout` discovers for the
# tui_binary health lane. The list itself is $COORD_FLEET_REPOS, in the core.
read -r -a REPOS <<< "$COORD_FLEET_REPOS"

WITH_GTK=0; WITH_BROWSER=0; SEED_CARGO_TARGET=0
while [[ $# -gt 0 ]]; do
    case $1 in
        --with-gtk)           WITH_GTK=1; shift ;;
        --with-browser)       WITH_BROWSER=1; shift ;;
        --seed-cargo-target)  SEED_CARGO_TARGET=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "must run as root" >&2; exit 1; }
log() { printf '\n=== %s ===\n' "$*"; }
as_coord() { sudo -u "$COORD_USER" -H bash -lc "$*"; }

# --------------------------------------------------------------------------
log "1/9  base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# $COORD_BASE_REQUIREMENTS (the core) is the shared list -- the same one the
# bare-metal lane probes -- and covers tmux, which coord/interactive.py,
# drive.py, terminal and reattach all shell out to: a worker without it fails
# at dispatch, not at build. The extras below are genuinely image-only:
# libssl-dev/ca-certificates/gnupg for building crates and verifying the apt
# keyrings this script adds, python3-pip because the venv is built offline of
# an operator.
mapfile -t _base_pkgs < <(coord_core_base_packages)
apt-get install -y -qq --no-install-recommends \
    "${_base_pkgs[@]}" \
    libssl-dev ca-certificates gnupg python3-pip

coord_core_python_meets_floor \
    || { echo "Python ${COORD_PYTHON_MIN_VERSION}+ required (install-agent.sh enforces this)" >&2; exit 1; }

if [[ $WITH_GTK -eq 1 ]]; then
    apt-get install -y -qq --no-install-recommends libgtk-4-dev
fi
if [[ $WITH_BROWSER -eq 1 ]]; then
    apt-get install -y -qq --no-install-recommends chromium-browser || \
        apt-get install -y -qq --no-install-recommends chromium
fi

# --------------------------------------------------------------------------
log "2/9  gh (official repo -- Ubuntu's own gh is far below the ${COORD_GH_MIN_VERSION} floor)"
coord_core_install_gh \
    || { echo "installing gh from the official GitHub CLI apt source failed" >&2; exit 1; }

# HARD-FAIL now rather than at the CI merge gate
# (coord.github_ops.GhTooOldForJsonChecks). `coord_core_gh_meets_floor` is
# false for a missing gh and for a gh whose --version says nothing, so this
# check is reachable in the failing direction -- it is not an absence
# defaulting to the permissive branch.
gh_ver="$(coord_core_gh_version)"
if ! coord_core_gh_meets_floor; then
    echo "gh ${gh_ver:-<absent>} is below the required $COORD_GH_MIN_VERSION floor" >&2; exit 1
fi
echo "gh $gh_ver OK (floor $COORD_GH_MIN_VERSION)"

# --------------------------------------------------------------------------
log "3/9  rust toolchain, system-wide at $COORD_RUST_HOME"
# MUST be system-wide -- see coord_core_install_rust_system_wide for #1671's
# root cause. The location and the install both live in the core; only the
# hard-fail wording is ours.
coord_core_install_rust_system_wide \
    || { echo "system-wide rust install did not yield a working cargo+rustc on /usr/local/bin" >&2; exit 1; }
cargo --version

# --------------------------------------------------------------------------
log "4/9  node ${COORD_NODE_MAJOR}.x + Claude Code CLI"
coord_core_install_node_system_wide \
    || { echo "node ${COORD_NODE_MAJOR}.x install did not put node on PATH" >&2; exit 1; }

# --------------------------------------------------------------------------
log "5/9  tailscale (installed, NOT authenticated)"
curl -fsSL https://tailscale.com/install.sh | sh
systemctl disable --now tailscaled 2>/dev/null || true
# Deliberately no `tailscale up`. Node identity is minted per-boot from the
# OAuth client as an ephemeral, pre-authorized, tag:coord-worker key. Baking
# tailscaled.state would give every VM the same node identity.

# --------------------------------------------------------------------------
log "6/9  ${COORD_USER} user"
if ! id -u "$COORD_USER" &>/dev/null; then
    useradd --create-home --shell /bin/bash "$COORD_USER"
fi
# systemd --user services must survive having nobody logged in.
loginctl enable-linger "$COORD_USER"

# npm global prefix -> ~/.local so `claude` lands on ~/.local/bin, which IS on
# the agent unit's PATH. A default /usr/lib/node_modules install is fine too,
# but this keeps the CLI updatable without root.
as_coord "mkdir -p ~/.local/bin && npm config set prefix ~/.local"
as_coord "npm install -g @anthropic-ai/claude-code"
as_coord "claude --version"

# --------------------------------------------------------------------------
log "7/9  opencode CLI ${COORD_OPENCODE_VERSION}, pinned to the standing fleet (#1777)"
# The pin, the ~/.opencode/bin PATH problem and the "never authenticate here"
# rule all live in the core (coord_core_opencode_install_cmd). What is
# image-specific -- and stays here -- is WHO runs it: the dedicated `coord`
# user whose home survives `waagent -deprovision+user`.
as_coord "$(coord_core_opencode_install_cmd)"
as_coord "$(coord_core_opencode_link_cmd)"
as_coord "opencode --version"

# --------------------------------------------------------------------------
log "8/9  coord venv (PyPI, NEVER editable) + repo clones + warm caches"
# INVARIANT (CLAUDE.md): ~/.coord-venv must be a PyPI install. An editable
# install makes `coord agent update` git-pull a checkout instead of upgrading,
# so released versions never propagate. Do not "improve" this to pip install -e.
as_coord "python3 -m venv ~/.coord-venv"
as_coord "~/.coord-venv/bin/pip install --upgrade pip -q"
# #1237: the `[server]` extra is mandatory on an agent — the base package is a
# client-only CLI and `coord agent` there refuses to boot.
as_coord "~/.coord-venv/bin/pip install --upgrade 'code-coordinator[server]' -q"
as_coord "~/.coord-venv/bin/coord version"

as_coord "mkdir -p ~/src"
for repo in "${REPOS[@]}"; do
    as_coord "git clone --filter=blob:none https://github.com/${COORD_GITHUB_ORG}/${repo}.git ~/src/${repo}"
done
# ~/src/<repo> is the worker WORKTREE BASE -- `git worktree add` runs from it.
# Never delete it to fix drift (CLAUDE.md); fix the install instead.

# Warm the crate registry: the dominant cold-start cost is fetching hundreds of
# crate sources, not compiling them.
# #2899: `coord-tui` is its own repo now, with a ROOT Cargo.toml like every
# other Rust checkout — so the `-f tui/Cargo.toml` special case that existed
# only for code-coordinator's nested crate is gone, and code-coordinator (now
# pure Python) drops out of this loop entirely.
for repo in coord-tui quadraui vimcode; do
    as_coord "cd ~/src/${repo} 2>/dev/null && [ -f Cargo.toml ] && cargo fetch --locked 2>/dev/null || true"
done

# Warm pip + npm caches for the coordinator's own dev/test deps.
as_coord "~/.coord-venv/bin/pip download -q -d /tmp/wheelwarm 'code-coordinator[dev]' 2>/dev/null || true; rm -rf /tmp/wheelwarm"
as_coord "cd ~/src/code-coordinator/coord/dashboard/webapp && npm ci --prefer-offline 2>/dev/null || true"

if [[ $SEED_CARGO_TARGET -eq 1 ]]; then
    # Opt-in: bake a compiled target/ so the first cargo build is incremental.
    # Saves ~15-25 min on first Rust task, but adds tens of GB to the image and
    # needs a larger OS disk. Boot copies it onto the free local NVMe.
    log "8b/9  seeding compiled cargo target (large)"
    install -d -o "$COORD_USER" "$CARGO_TARGET_SEED"
    as_coord "cd ~/src/coord-tui && CARGO_TARGET_DIR=$CARGO_TARGET_SEED cargo build || true"
    du -sh "$CARGO_TARGET_SEED" || true
fi

# --------------------------------------------------------------------------
log "9/9  verify prereqs (mirrors coord/prereqs.py)"
# The baseline list and the per-tool check are $COORD_PREREQ_CHECKS /
# coord_core_check_tool in the core, cross-checked against the real
# coord.prereqs.BASELINE_PREREQS by tests/test_provision_core.py. Only the
# image-lane extras -- the optional stacks this build was asked for, and the
# two tools that must resolve as the `coord` user rather than as root -- are
# below.
fail=0
coord_core_verify_baseline_prereqs || fail=1
[[ $WITH_GTK     -eq 1 ]] && { coord_core_check_tool gtk4    pkg-config --modversion gtk4 || fail=1; }
[[ $WITH_BROWSER -eq 1 ]] && { coord_core_check_tool browser chromium --version          || fail=1; }
as_coord "command -v claude >/dev/null" \
    && printf '  %-10s %s\n' claude "$(as_coord 'claude --version' 2>&1 | head -1)" \
    || { printf '  %-10s MISSING\n' claude; fail=1; }

# `command -v` here resolves through the coord user's login-shell PATH, which
# --no-modify-path deliberately did NOT extend with ~/.opencode/bin -- so this
# only succeeds via the ~/.local/bin symlink above, the same directory the
# coord-agent unit's PATH already covers. A pass here really means "the agent
# process can find it," not just "the binary exists somewhere on disk."
if as_coord "command -v opencode >/dev/null"; then
    oc_ver="$(as_coord 'opencode --version' 2>&1 | tail -1)"
    if coord_core_opencode_version_matches "$oc_ver"; then
        printf '  %-10s %s\n' opencode "$oc_ver"
    else
        printf '  %-10s VERSION MISMATCH (got %s, want %s)\n' \
            opencode "${oc_ver:-<nothing>}" "$COORD_OPENCODE_VERSION"
        fail=1
    fi
else
    printf '  %-10s MISSING\n' opencode; fail=1
fi

[[ $fail -eq 0 ]] || { echo "PREREQ CHECK FAILED -- do not generalize this image" >&2; exit 1; }

log "provisioning complete -- now run scrub-and-generalize.sh"
