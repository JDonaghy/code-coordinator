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

COORD_USER="${COORD_USER:-coord}"
GH_MIN_VERSION="2.86.0"       # coord.github_ops.GH_PR_CHECKS_JSON_MIN_VERSION
NODE_MAJOR="22"
OPENCODE_VERSION="1.18.11"    # pinned to match the standing fleet -- see #1777
RUST_HOME="/opt/rust"
CARGO_TARGET_SEED="/opt/cargo-target-seed"
# #2899: coord-tui joins the clone list as its own repo. `~/src/coord-tui` is
# what `coordinator.yml`'s `repo_paths.coord-tui` names on every machine that
# carries it, and what `resolve_coord_tui_checkout` discovers for the
# tui_binary health lane.
REPOS=(code-coordinator coord-tui quadraui vimcode)
GITHUB_ORG="JDonaghy"

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
apt-get install -y -qq --no-install-recommends \
    build-essential pkg-config libssl-dev ca-certificates curl gnupg unzip \
    git jq tmux ripgrep rsync \
    python3 python3-venv python3-pip
# coord/interactive.py, drive.py, terminal, reattach all shell out to tmux --
# a worker without it fails at dispatch, not at build.

python3 --version | grep -qE '3\.(1[2-9]|[2-9][0-9])' \
    || { echo "Python 3.12+ required (install-agent.sh enforces this)" >&2; exit 1; }

if [[ $WITH_GTK -eq 1 ]]; then
    apt-get install -y -qq --no-install-recommends libgtk-4-dev
fi
if [[ $WITH_BROWSER -eq 1 ]]; then
    apt-get install -y -qq --no-install-recommends chromium-browser || \
        apt-get install -y -qq --no-install-recommends chromium
fi

# --------------------------------------------------------------------------
log "2/9  gh (official repo -- Ubuntu's own gh is far below the ${GH_MIN_VERSION} floor)"
install -d -m 0755 /usr/share/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg status=none
chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
apt-get update -qq
apt-get install -y -qq gh

# Hard-fail now rather than at the CI merge gate (coord.github_ops.GhTooOldForJsonChecks).
gh_ver="$(gh --version | sed -n 's/^gh version \([0-9.]*\).*/\1/p')"
if [[ "$(printf '%s\n%s\n' "$GH_MIN_VERSION" "$gh_ver" | sort -V | head -1)" != "$GH_MIN_VERSION" ]]; then
    echo "gh $gh_ver is below the required $GH_MIN_VERSION floor" >&2; exit 1
fi
echo "gh $gh_ver OK (floor $GH_MIN_VERSION)"

# --------------------------------------------------------------------------
log "3/9  rust toolchain, system-wide"
# MUST be system-wide. install-agent.sh pins the agent unit's PATH to
#   $VENV/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin
# -- note the absence of ~/.cargo/bin. Workers inherit that PATH, so a
# per-user rustup install leaves `cargo` invisible to every dispatched task.
export RUSTUP_HOME="$RUST_HOME" CARGO_HOME="$RUST_HOME"
curl -fsSL https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal \
    --default-toolchain stable
for bin in "$RUST_HOME"/bin/*; do ln -sf "$bin" "/usr/local/bin/$(basename "$bin")"; done
chmod -R a+rX "$RUST_HOME"
cat > /etc/profile.d/rust.sh <<EOF
export RUSTUP_HOME=$RUST_HOME
export CARGO_HOME=\$HOME/.cargo
EOF
cargo --version

# --------------------------------------------------------------------------
log "4/9  node ${NODE_MAJOR}.x + Claude Code CLI"
curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
apt-get install -y -qq nodejs

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
log "7/9  opencode CLI ${OPENCODE_VERSION}, pinned to the standing fleet (#1777)"
# The official installer always drops the binary at ~/.opencode/bin -- there
# is no env var or flag that redirects it (verified against install.sh
# source, 2026-08-03: `INSTALL_DIR=$HOME/.opencode/bin` is unconditional,
# --binary only changes the SOURCE, not the destination). Rather than fight
# that, symlink into ~/.local/bin, which the coord-agent unit's PATH already
# includes (see deploy/coord-agent.service). That is what makes this land on
# the *agent process's* PATH without a coord-agent.service.d PATH drop-in --
# the standing fleet needed one (20-opencode-path.conf) precisely because
# nothing else put ~/.opencode/bin on that PATH.
#
# Do NOT authenticate here (no `opencode auth login`, no auth.json). A golden
# image must contain zero identity, same rule as the tailscale step above --
# the credential arrives at boot from Key Vault (see bootstrap-shared.sh /
# docs/OPENCODE_VERIFICATION.md for the verified non-interactive path).
as_coord "curl -fsSL https://opencode.ai/install | bash -s -- --version ${OPENCODE_VERSION} --no-modify-path"
as_coord "ln -sf ~/.opencode/bin/opencode ~/.local/bin/opencode"
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
    as_coord "git clone --filter=blob:none https://github.com/${GITHUB_ORG}/${repo}.git ~/src/${repo}"
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
fail=0
check() { # name binary args... -- prints version or marks failure
    local name="$1"; shift
    if command -v "$1" >/dev/null 2>&1; then
        printf '  %-10s %s\n' "$name" "$("$@" 2>&1 | head -1)"
    else
        printf '  %-10s MISSING\n' "$name"; fail=1
    fi
}
check git    git --version
check gh     gh --version
check cargo  cargo --version
check python3 python3 --version
check tmux   tmux -V
check node   node --version
[[ $WITH_GTK     -eq 1 ]] && check gtk4    pkg-config --modversion gtk4
[[ $WITH_BROWSER -eq 1 ]] && check browser chromium --version
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
    if [[ "$oc_ver" == "$OPENCODE_VERSION" ]]; then
        printf '  %-10s %s\n' opencode "$oc_ver"
    else
        printf '  %-10s VERSION MISMATCH (got %s, want %s)\n' opencode "$oc_ver" "$OPENCODE_VERSION"
        fail=1
    fi
else
    printf '  %-10s MISSING\n' opencode; fail=1
fi

[[ $fail -eq 0 ]] || { echo "PREREQ CHECK FAILED -- do not generalize this image" >&2; exit 1; }

log "provisioning complete -- now run scrub-and-generalize.sh"
