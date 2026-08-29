#!/usr/bin/env bash
set -euo pipefail

# Defaults
VENV_DIR="$HOME/.coord-venv"
MACHINE_NAME=""
PORT=7433
# #1237: the `[server]` extra is MANDATORY on an agent. The base package is a
# client-only CLI (no starlette/uvicorn), so `coord agent` on a bare install
# refuses to boot with an "install the [server] extra" message.
# #2104: the distribution renamed `claude-coordinator` -> `code-coordinator`.
# The venv, the `coord` entrypoint and the unit name below are all unchanged —
# only the name pip resolves against moved. A host that already has the old
# distribution in $VENV_DIR keeps it (pip installs the new name alongside);
# `coord/dist_name.py` resolves whichever is present, preferring the new one.
INSTALL_SOURCE="code-coordinator[server]"  # PyPI package name + server extra
# Fall back to GitHub install if PyPI isn't published yet. Still the OLD repo
# path on purpose: GitHub redirects a renamed repo's old URL indefinitely, so
# this keeps working both before and after the repo rename (#2104), whereas
# the new path 404s until that rename actually lands.
GITHUB_REPO="https://github.com/JDonaghy/claude-coordinator.git"

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --machine) MACHINE_NAME="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --from-github) INSTALL_SOURCE="code-coordinator[server] @ git+${GITHUB_REPO}"; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "=== code-coordinator agent installer ==="

# Check Python 3.12+
python3 --version | grep -qE "3\.(1[2-9]|[2-9][0-9])" || {
    echo "error: Python 3.12+ required"; exit 1
}
# Used later to size the apt hint if `python3 -m venv` fails; failure here
# just means the hint falls back to the unversioned package name.
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "")

# Check claude CLI
which claude >/dev/null 2>&1 || {
    echo "warning: 'claude' CLI not found on PATH"
    echo "  Workers need Claude Code CLI installed. Install it before starting the agent."
}

# Create/update venv
#
# #2911: a first run that fails partway (e.g. Ubuntu 24.04 shipping python3
# without ensurepip) can leave $VENV_DIR existing with a bin/ that has
# python3 but no pip. Trusting `-d "$VENV_DIR"` alone then skips
# `python3 -m venv` on retry and the script dies later at
# "$VENV_DIR/bin/pip: No such file or directory" - a message that names pip,
# not the actual problem. Validate the venv is actually usable instead of
# just present, and recreate it if not. Also trap failures so a venv this
# run itself created (but never finished installing into) doesn't poison
# the next retry either.
CREATED_VENV=0
on_exit() {
    local status=$?
    if [ "$status" -ne 0 ] && [ "$CREATED_VENV" -eq 1 ] && [ -d "$VENV_DIR" ]; then
        echo "" >&2
        echo "install failed - removing the venv this run created at $VENV_DIR" >&2
        echo "so the next attempt starts clean instead of finding a partial one." >&2
        rm -rf "$VENV_DIR"
    fi
    exit "$status"
}
trap on_exit EXIT

if [ -d "$VENV_DIR" ] && [ -x "$VENV_DIR/bin/pip" ]; then
    echo "Updating existing installation at $VENV_DIR..."
else
    if [ -d "$VENV_DIR" ]; then
        echo "Existing $VENV_DIR has no bin/pip (partial or failed install) - recreating..."
        # Note: if a `coord agent update` blue/green swap (coord/agent_update.py)
        # has ever run here, $VENV_DIR is normally a symlink to
        # .coord-venv.blue/.coord-venv.green, not a plain directory — but a
        # symlink with a working bin/pip would have taken the branch above,
        # so reaching here means it's either a plain broken directory (the
        # common first-install case this fix targets) or an already-broken
        # symlink. `rm -rf` on a symlink unlinks just the symlink, not its
        # target, which would strand the real blue/green slot on disk; that's
        # an existing-corruption corner case outside a first-install script's
        # scope, not something this fix introduces.
        rm -rf "$VENV_DIR"
    fi
    echo "Creating virtual environment at $VENV_DIR..."
    CREATED_VENV=1
    if ! python3 -m venv "$VENV_DIR"; then
        echo "" >&2
        echo "error: 'python3 -m venv' failed to create $VENV_DIR." >&2
        echo "  On Debian/Ubuntu this usually means venv/ensurepip support isn't installed:" >&2
        if [ -n "$PY_VERSION" ]; then
            echo "    sudo apt install python3-venv python${PY_VERSION}-venv" >&2
        else
            echo "    sudo apt install python3-venv" >&2
        fi
        exit 1
    fi
fi

# Install/upgrade
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install --upgrade "$INSTALL_SOURCE" -q
echo "Installed: $("$VENV_DIR/bin/coord" version)"

# The venv is now fully installed and verified working (coord version printed
# above). Disarm the cleanup trap here: everything from this point on
# (machine-name detection, the systemd unit, the coord CLI shim, node shims,
# systemctl, loginctl) is unrelated to venv creation, and a failure in any of
# it must NOT delete a venv that already works — that would poison the retry
# with a from-scratch pip re-install just to get back to the same later
# failure (#2911 review).
trap - EXIT
CREATED_VENV=0

# --- coord CLI shim (#2936) --------------------------------------------------
# Workers are spawned with THIS agent's own venv stripped from PATH (#402,
# hardened by #2569's PIP_REQUIRE_VIRTUALENV after an 11h fleet outage — see
# coord/agent.py's `_worker_subprocess_env`). That strip is correct and must
# stay: a worker's bare `pip install -e .` must never land in the fleet's
# live $VENV_DIR. But nothing else guarantees a worker's PATH resolves
# `coord` itself — and when it doesn't, a smoke/test worker can run its
# whole suite, pass, and be structurally unable to record the verdict via
# `coord test <id> --passed`. The missing verdict then reads as a TEST
# FAILURE and walks the model-escalation ladder for an infrastructure gap,
# never a weak model (#2936; one instance cost an extra opus rerun of an
# already-passing sonnet leg).
#
# $HOME/.local/bin is (a) already put on the coord-agent unit's PATH below
# and (b) NOT stripped from a worker's PATH — #2569's strip matches
# $VENV_DIR/bin by name/realpath only, never $HOME/.local/bin. A plain
# symlink there closes the gap for every worker this agent spawns.
#
# MUST point at $VENV_DIR itself — the blue/green symlink `coord agent
# update` repoints atomically on every release (docs/AGENT_OPERATIONS.md) —
# NOT at a `readlink -f`-resolved `.blue`/`.green` path. Resolving it here
# would freeze the shim on whichever side happens to be live at install
# time and silently go stale on the very next blue/green swap.
COORD_SHIM_DIR="$HOME/.local/bin"
COORD_SHIM_TARGET="$VENV_DIR/bin/coord"
COORD_SHIM_LINK="$COORD_SHIM_DIR/coord"
mkdir -p "$COORD_SHIM_DIR"
if [ -L "$COORD_SHIM_LINK" ] && [ "$(readlink "$COORD_SHIM_LINK")" = "$COORD_SHIM_TARGET" ]; then
    : # already ours, and already pointed at the unresolved blue/green symlink
elif [ -e "$COORD_SHIM_LINK" ] || [ -L "$COORD_SHIM_LINK" ]; then
    echo "warning: $COORD_SHIM_LINK exists and is not the coord shim — left as-is"
    echo "  A smoke/test worker on this machine cannot record its verdict via"
    echo "  'coord test <id> --passed' unless 'coord' resolves on ITS PATH (#2936)."
else
    ln -s "$COORD_SHIM_TARGET" "$COORD_SHIM_LINK"
    echo "Installed coord shim: $COORD_SHIM_LINK -> $COORD_SHIM_TARGET"
fi

# Detect machine name if not provided
if [ -z "$MACHINE_NAME" ]; then
    MACHINE_NAME=$(hostname -s)
    echo "Machine name (from hostname): $MACHINE_NAME"
    echo "  Override with: --machine NAME"
fi

# Create systemd user unit
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/coord-agent.service" << UNIT
[Unit]
Description=Coordinator agent server (port $PORT)
After=network-online.target
# #2938: bound Restart=always below so a genuinely broken binary fails loud
# (systemd 'failed'/start-limit-hit) instead of crash-looping silently
# forever — see deploy/coord-agent.service's header for the full incident.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
ExecStart=$VENV_DIR/bin/coord agent --machine $MACHINE_NAME --port $PORT
# #2938: was Restart=on-failure. POST /update stops this process cleanly so
# it can come back on the swapped venv via an explicit self-restart
# (coord/agent_app.py); if that explicit restart is ever lost, on-failure
# never fires for a clean exit and the unit is gone for good, port 7433
# refused, no remote-control channel left. Restart=always is the backstop.
Restart=always
RestartSec=5
# #1671: include ~/.cargo/bin so a rustup-installed toolchain resolves to
# both this agent process (the "rust" capability probe, coord/prereqs.py)
# and to workers it spawns (#402: worker PATH derives from the agent's,
# venv stripped) — see deploy/coord-agent.service for the false-green trap
# this guards against (finding cargo but not rustc).
Environment=PATH=$VENV_DIR/bin:$HOME/.cargo/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
UNIT

# --- Node shims (#1678) ------------------------------------------------------
# The `browser` capability — Playwright acceptance suites such as the
# `coord-web` repo's `npm run test:e2e` (that suite lived at
# coord/dashboard/webapp/ here until #2009 moved it out) — needs `node`/`npm`
# on the agent's PATH, and therefore on every worker's PATH (#402).
#
# Unlike #1671's ~/.cargo/bin, that directory CANNOT simply be appended to the
# PATH line above: nvm installs Node into a version-stamped directory
# (~/.nvm/versions/node/vX.Y.Z/bin), so pinning it would work exactly until the
# next `nvm install` and then silently stop — the capability would read unmet
# again with no board-visible reason. Instead install run-time-resolving shims
# into ~/.local/bin, which is already on the PATH line above, so the PATH
# mechanism itself is untouched and a Node version bump is a no-op.
#
# Keep this heredoc BYTE-IDENTICAL to deploy/node-shim.sh —
# tests/test_node_shim.py asserts it (and asserts neither embeds a vX.Y.Z).
SHIM_DIR="$HOME/.local/bin"
SHIM_PATH="$SHIM_DIR/coord-node-shim"
mkdir -p "$SHIM_DIR"
cat > "$SHIM_PATH" << 'NODE_SHIM'
#!/usr/bin/env bash
# coord node shim — resolve Node at RUN time, never at install time (#1678).
#
# Installed by install-agent.sh as ~/.local/bin/coord-node-shim, with
# node/npm/npx symlinked at it. That directory is ALREADY on the coord agent
# unit's PATH (deploy/coord-agent.service), so this file needs no change to
# the PATH mechanism #1671 shipped — it only makes Node resolvable through a
# directory that is already there, for the agent and for every worker it
# spawns (#402: a worker's PATH is the agent's, venv stripped).
#
# Why a shim rather than another PATH entry: nvm installs into a
# VERSION-STAMPED directory (~/.nvm/versions/node/vX.Y.Z/bin). Baking that
# into the unit works exactly until the next `nvm install`, at which point
# the `browser` capability silently goes unmet again and webapp Test stages
# resume the invisible 30s refusal loop — a fresh instance of the very bug
# this shim closes. Re-resolving on each invocation makes a Node version
# bump a no-op.
#
# Dispatch is by argv[0]: whatever name you invoke it under is the binary it
# execs from the resolved Node bin directory.
#
# tests/test_node_shim.py asserts (a) this file embeds no vX.Y.Z literal and
# (b) install-agent.sh ships a byte-identical copy.

set -uo pipefail

self="${0##*/}"
shim_dir="$(cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)" || shim_dir=""
nvm_dir="${NVM_DIR:-$HOME/.nvm}"
versions_dir="$nvm_dir/versions/node"

resolved=""

# 1. nvm's `default` alias, when it names an installed version outright.
#    (It may instead hold a symbolic name such as `node` or `lts/*`; those
#    fall through to the newest-installed scan below, which is how nvm
#    itself resolves `default -> node`.)
if [ -r "$nvm_dir/alias/default" ]; then
    want="$(tr -d '[:space:]' < "$nvm_dir/alias/default" 2>/dev/null)"
    if [ -n "$want" ]; then
        for cand in "$want" "v$want"; do
            if [ -x "$versions_dir/$cand/bin/node" ]; then
                resolved="$versions_dir/$cand/bin"
                break
            fi
        done
    fi
fi

# 2. Otherwise the highest installed version. `sort -V` is GNU coreutils and
#    recent BSD; fall back to lexical order where it is unsupported.
if [ -z "$resolved" ] && [ -d "$versions_dir" ]; then
    installed="$(ls -1 "$versions_dir" 2>/dev/null | sort -V 2>/dev/null)"
    [ -n "$installed" ] || installed="$(ls -1 "$versions_dir" 2>/dev/null | sort)"
    while IFS= read -r cand; do
        [ -n "$cand" ] || continue
        [ -x "$versions_dir/$cand/bin/node" ] && resolved="$versions_dir/$cand/bin"
    done <<EOF
$installed
EOF
fi

if [ -n "$resolved" ] && [ -x "$resolved/$self" ]; then
    exec "$resolved/$self" "$@"
fi

# 3. Last resort: a system install elsewhere on PATH. Skip this shim's own
#    directory so a missing Node exits honestly instead of exec-looping.
IFS=':' read -r -a _shim_path_dirs <<< "${PATH:-}"
for dir in "${_shim_path_dirs[@]}"; do
    [ -n "$dir" ] || continue
    real="$(cd -- "$dir" 2>/dev/null && pwd -P)" || continue
    [ -n "$shim_dir" ] && [ "$real" = "$shim_dir" ] && continue
    if [ -x "$real/$self" ]; then
        exec "$real/$self" "$@"
    fi
done

echo "coord node shim: no '$self' found (NVM_DIR=$nvm_dir, and none on \$PATH)" >&2
exit 127
NODE_SHIM
chmod +x "$SHIM_PATH"

for shim_name in node npm npx; do
    link="$SHIM_DIR/$shim_name"
    if [ -L "$link" ] && [ "$(readlink "$link")" = "$SHIM_PATH" ]; then
        continue  # already ours
    fi
    if [ -e "$link" ] || [ -L "$link" ]; then
        echo "warning: $link exists and is not the coord node shim — left as-is"
        continue
    fi
    ln -s "$SHIM_PATH" "$link"
    echo "Installed Node shim: $link -> $SHIM_PATH"
done

if NODE_VERSION=$(PATH="$SHIM_DIR:/usr/local/bin:/usr/bin:/bin" "$SHIM_DIR/node" --version 2>/dev/null); then
    echo "Node resolves for the agent: $NODE_VERSION"
else
    echo "warning: no Node found on this machine."
    echo "  The 'browser' capability (Playwright acceptance suites) will read"
    echo "  unmet in 'coord doctor' until Node is installed — e.g."
    echo "  'nvm install --lts'. The shims pick it up with no further changes."
fi

# Enable and start
systemctl --user daemon-reload
systemctl --user enable coord-agent
systemctl --user restart coord-agent

# Enable lingering so the service runs even when not logged in
loginctl enable-linger "$(whoami)" 2>/dev/null || true

echo ""
echo "=== Agent installed and running ==="
echo "  Machine: $MACHINE_NAME"
echo "  Port: $PORT"
echo "  Service: systemctl --user status coord-agent"
echo "  Logs: journalctl --user -u coord-agent -f"
echo ""
echo "Next steps:"
echo "  1. Ensure coordinator.yml exists on the coordinator machine with this machine listed"
echo "  2. Run 'coord status' from the coordinator to verify connectivity"
echo ""
echo "To update later: re-run this script (it's idempotent)"
