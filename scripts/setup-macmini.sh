#!/usr/bin/env bash
#
# setup-macmini.sh — provision a Mac mini as a code-coordinator fleet agent.
#
# Derived from docs/MAC_MINI.md (provisioning runbook), install-agent.sh
# (the Linux source of truth for the venv + shims), deploy/coord-agent.service
# (#1671 PATH rules), and docs/GRAPHIFY_SETUP.md.
#
# This is the macOS counterpart to install-agent.sh, which does NOT apply here:
# it generates a systemd user unit and calls loginctl. Service supervision on
# macOS is launchd, which is issue #1158 (CP-3) and UNBUILT — there is no
# checked-in plist in deploy/. Step 12 writes an interim hand-rolled one but
# deliberately does not load it.
#
# Idempotent: safe to re-run. Nothing here is destructive; every step that
# would overwrite something checks first and warns instead.
#
# Run it ON the mac, in an INTERACTIVE session. Two steps shell out to `gh`
# and `git` against an https remote, and both read the login KEYCHAIN, which
# a non-interactive ssh session cannot unlock (see docs/MAC_MINI.md ->
# "Provisioning traps found in practice"). Everything else works fine over
# ssh; --skip-clones makes the whole script ssh-safe.
#
# First run: 2026-09-06, on an M4 mini (macOS 26.4.1 arm64) joined as
# `macmini`. What it produced is recorded in docs/MAC_MINI.md.
#
# Usage:
#   ./setup-macmini.sh                 # everything except sudo + launchd steps
#   ./setup-macmini.sh --with-sudo     # also do Remote Login + no-sleep
#   ./setup-macmini.sh --with-launchd  # also write (not load) the agent plist
#   ./setup-macmini.sh --skip-clones   # skip the git clone step
#   ./setup-macmini.sh --machine NAME  # machine name in coordinator.yml (default: macmini)

set -euo pipefail

MACHINE_NAME="macmini"
AGENT_PORT=7433
CARGO_JOBS=6
DO_SUDO=0
DO_LAUNCHD=0
DO_CLONES=1

SRC_DIR="$HOME/src"
VENV_DIR="$HOME/.coord-venv"
# coord/platform_paths.py:30 — on darwin the state root resolves through
# platformdirs, NOT to ~/.coord. Getting this wrong does not error: the
# agent starts, answers /health, and publishes ZERO capabilities.
COORD_DIR="$HOME/Library/Application Support/coord"
COORD_COMPAT_LINK="$HOME/.coord"   # so fleet runbook paths still resolve
SHIM_DIR="$HOME/.local/bin"
SETTINGS_DIR="$SRC_DIR/coord-settings"

# Repo NAME -> clone directory. The name and the directory differ for
# code-coordinator on purpose (#2104) and coordinator.yml's repo_paths must
# match the DIRECTORY. Everything else is 1:1.
REPOS=(
  vimcode
  quadraui
  code-coordinator
  coord-portal
  stick-demo
  space-invaders
  natal-chart
  coord-web
  coord-tui
  grocery-list
  format-converter
  coord-settings          # not a work repo — holds the tracked coordinator.yml
)

RESIDUE=()
step()    { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()      { printf '  \033[32mok\033[0m   %s\n' "$*"; }
skip()    { printf '  --   %s\n' "$*"; }
warn()    { printf '  \033[33mwarn\033[0m %s\n' "$*"; RESIDUE+=("$*"); }
die()     { printf '\n\033[31merror\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-sudo)    DO_SUDO=1; shift ;;
    --with-launchd) DO_LAUNCHD=1; shift ;;
    --skip-clones)  DO_CLONES=0; shift ;;
    --machine)      MACHINE_NAME="$2"; shift 2 ;;
    --port)         AGENT_PORT="$2"; shift 2 ;;
    -h|--help)      sed -n '2,25p' "$0"; exit 0 ;;
    *)              die "unknown option: $1" ;;
  esac
done

# ── 0. Preflight ────────────────────────────────────────────────────────────
step "0. Preflight"

[[ "$(uname -s)" == "Darwin" ]] || die "this script is macOS-only (uname -s = $(uname -s))"
[[ "$(id -u)" != "0" ]]         || die "do not run as root — this installs into \$HOME"

ok "macOS $(sw_vers -productVersion) on $(uname -m)"

command -v brew >/dev/null || die "Homebrew not found on PATH"
BREW_PREFIX="$(brew --prefix)"
ok "homebrew at $BREW_PREFIX"

# gh is only needed for the clone step. It is checked leniently otherwise
# because on macOS gh keeps its token in the LOGIN KEYCHAIN, which a
# non-interactive ssh session cannot unlock — so `gh auth status` fails there
# even when gh is perfectly authenticated for the interactive user. Gating
# every run on it makes the script unrunnable over ssh for no reason.
if ! command -v gh >/dev/null; then
  [[ "$DO_CLONES" == "1" ]] && die "gh not found on PATH (needed for --skip-clones=off)"
  skip "gh not on PATH — not needed, clones are skipped"
elif gh auth status >/dev/null 2>&1; then
  ok "gh authenticated"
elif [[ "$DO_CLONES" == "1" ]]; then
  die "gh is not authenticated — run 'gh auth login' first.
       If you are seeing this over ssh, that is the macOS keychain, not gh:
       re-run in an interactive session on the box, or pass --skip-clones."
else
  skip "gh auth unreadable (keychain-locked ssh session?) — not needed, clones are skipped"
fi

if command -v tailscale >/dev/null; then
  TS_NAME="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
  if [[ -n "${TS_NAME:-}" ]]; then
    ok "tailnet name: $TS_NAME"
    echo "       ^ this is the value for coordinator.yml's  host:"
  else
    warn "could not read this node's MagicDNS name from tailscale status"
  fi
else
  warn "tailscale CLI not on PATH — cannot confirm the tailnet name"
fi

# Xcode CLT: rust and every -sys crate need it. The installer is a GUI
# dialog, so this can only check and instruct.
if xcode-select -p >/dev/null 2>&1; then
  ok "xcode command line tools at $(xcode-select -p)"
else
  echo "  Xcode Command Line Tools are missing. A GUI installer will open."
  xcode-select --install || true
  die "re-run this script once the Command Line Tools install finishes"
fi

# ── 1. macOS server hygiene ─────────────────────────────────────────────────
step "1. macOS server hygiene"

# Reading power state needs NO privileges, so this ALWAYS runs and always
# reports the truth — even without --with-sudo. That split matters: on
# 2026-09-06 a first-provisioned mini went to idle sleep mid-leg, dropped off
# the tailnet, and killed a running worker. The leg was redispatched and the
# work survived only because the branch had already been pushed. The script
# had "skipped (pass --with-sudo)" and said nothing about sleep at all, so
# nothing connected the dispatch failure to a power setting.
#
# A fleet machine that naps is worse than one you cannot ssh to: the ssh gap
# announces itself the first time you try, the sleep gap only shows up as a
# worker dying with no explanation.
pm_get() { pmset -g 2>/dev/null | awk -v k="$1" '$1 == k { print $2; exit }'; }

SLEEP_NOW=$(pm_get sleep)
DISKSLEEP_NOW=$(pm_get disksleep)
POWERNAP_NOW=$(pm_get powernap)
AUTORESTART_NOW=$(pm_get autorestart)
WOMP_NOW=$(pm_get womp)
echo "  current: sleep=${SLEEP_NOW:-?} disksleep=${DISKSLEEP_NOW:-?} powernap=${POWERNAP_NOW:-?} autorestart=${AUTORESTART_NOW:-?} womp=${WOMP_NOW:-?}"

if [[ "$DO_SUDO" == "1" ]]; then
  # Remote Login is OFF by default. Without it: install/repair over ssh,
  # `coord machine doctor --ssh`, and every recovery path have no channel to
  # the machine. Note `coord release propagate` does NOT use ssh (it is an
  # HTTP POST /update to the agent) — which is exactly why ssh is the channel
  # you need when propagation is what stranded the agent.
  if sudo systemsetup -setremotelogin on 2>/dev/null; then
    ok "Remote Login (sshd) enabled"
  else
    warn "could not enable Remote Login — modern macOS requires Full Disk Access
       for the terminal app. Grant it in System Settings > Privacy & Security >
       Full Disk Access, or flip the switch at Settings > General > Sharing >
       Remote Login."
  fi

  # It is a server now. `sleep 0` alone is not enough — disksleep and
  # powernap both produce the same symptom from the fleet's side (a machine
  # that stops answering mid-leg), and autorestart is what brings it back
  # after a power cut rather than leaving it dark until someone notices.
  if sudo pmset -a sleep 0 disablesleep 1 disksleep 0 powernap 0 womp 1 autorestart 1 2>/dev/null; then
    ok "power: sleep/disksleep/powernap off, wake-on-net + autorestart on"
  else
    warn "could not apply pmset settings — run by hand:
       sudo pmset -a sleep 0 disablesleep 1 disksleep 0 powernap 0 womp 1 autorestart 1"
  fi
else
  skip "skipped (pass --with-sudo to enable Remote Login + harden power)"
  RESIDUE+=("Remote Login is off — ssh to this box is refused until you enable it.")
  # Name the specific defect, not just the skipped step. `sleep` is the one
  # that silently eats a dispatch.
  if [[ "${SLEEP_NOW:-1}" != "0" ]]; then
    RESIDUE+=("*** THIS MACHINE WILL SLEEP (pmset sleep=${SLEEP_NOW:-?}) ***
       It will drop off the tailnet while idle and KILL any running worker —
       the leg fails with no explanation that points at power. Fix before
       taking any dispatch:
         sudo pmset -a sleep 0 disablesleep 1 disksleep 0 powernap 0 womp 1 autorestart 1")
  else
    RESIDUE+=("Power settings not hardened (sleep is already 0, but disksleep=${DISKSLEEP_NOW:-?}
       powernap=${POWERNAP_NOW:-?} autorestart=${AUTORESTART_NOW:-?}). Same command as above.")
  fi
fi

# Auto-login: LaunchAgents start at LOGIN, not boot (see step 12). Without it
# the agent does not come back after a reboot — and from the coordinator that
# is indistinguishable from the machine being switched off. Best-effort read;
# an unreadable pref is reported as unknown, never as a defect.
# Distinguish "definitely off" from "could not tell": read the whole domain
# first. If THAT fails we have no basis for an opinion and say so — a check
# that cries wolf on a healthy machine is how a real defect gets ignored.
if ! defaults read /Library/Preferences/com.apple.loginwindow >/dev/null 2>&1; then
  skip "auto-login state unknown (loginwindow prefs unreadable) — verify by hand"
elif AUTOLOGIN=$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null) \
     && [[ -n "$AUTOLOGIN" ]]; then
  ok "auto-login enabled for '$AUTOLOGIN' — the agent returns after a reboot"
else
  # Domain readable + key absent = genuinely off. /etc/kcpassword is the
  # companion marker; its absence corroborates.
  warn "auto-login is OFF. LaunchAgents start at LOGIN, not boot, so coord-agent
       will NOT come back after a reboot — an OS update is enough to do it, and
       from the coordinator that is indistinguishable from the machine being
       switched off. Enable it: System Settings > Users & Groups > Automatic
       login. Until then, treat every reboot as attended."
  RESIDUE+=("auto-login is OFF — coord-agent does not survive a reboot unattended.")
fi

# ── 2. Homebrew packages ────────────────────────────────────────────────────
step "2. Homebrew packages"

# python@3.12: coord requires 3.12+, macOS system python3 is older.
# node: the browser/webapp lanes and the Claude Code CLI's ecosystem.
# pipx: graphify's documented install path.
# tmux: the interim way to supervise the agent until #1158 lands.
for pkg in python@3.12 node pipx tmux; do
  if brew list --versions "$pkg" >/dev/null 2>&1; then
    skip "$pkg already installed"
  else
    brew install "$pkg" && ok "installed $pkg"
  fi
done
brew list --versions gtk4 >/dev/null 2>&1 \
  && ok "gtk4 present" \
  || skip "gtk4 NOT installed — deliberate. GTK4-on-quartz behaves differently
       enough that a 'gtk' capability here would be a lie for visual work
       (docs/MAC_MINI.md). Install it only when macOS GTK is a real target."

PYTHON312="$BREW_PREFIX/bin/python3.12"
[[ -x "$PYTHON312" ]] || die "expected python3.12 at $PYTHON312 after brew install"
ok "python: $("$PYTHON312" --version)"

# ── 3. Rust ─────────────────────────────────────────────────────────────────
step "3. Rust toolchain"

if [[ -x "$HOME/.cargo/bin/cargo" ]]; then
  skip "rustup already installed: $("$HOME/.cargo/bin/cargo" --version)"
else
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path
  ok "installed $("$HOME/.cargo/bin/cargo" --version)"
fi

# Cap parallel codegen so a build spike cannot blow past 16GB of unified
# memory (docs/MAC_MINI.md sizing).
CARGO_CFG="$HOME/.cargo/config.toml"
if [[ -f "$CARGO_CFG" ]] && grep -q '^\[build\]' "$CARGO_CFG"; then
  skip "$CARGO_CFG already has a [build] section — left alone (check jobs = $CARGO_JOBS by hand)"
else
  printf '[build]\njobs = %s\n' "$CARGO_JOBS" >> "$CARGO_CFG"
  ok "set build.jobs = $CARGO_JOBS in $CARGO_CFG"
fi

# ── 4. Claude Code CLI ──────────────────────────────────────────────────────
step "4. Claude Code CLI"

if command -v claude >/dev/null 2>&1 || [[ -x "$HOME/.local/bin/claude" ]]; then
  skip "claude already installed"
else
  curl -fsSL https://claude.ai/install.sh | bash || warn "claude installer failed"
fi

if [[ -x "$HOME/.local/bin/claude" ]]; then
  ok "claude at $HOME/.local/bin/claude"
else
  warn "claude did not land at ~/.local/bin/claude — the fleet's agents assume
       that path. Find it and make sure the agent's PATH (step 12) reaches it."
fi
RESIDUE+=("Log in to Claude Code INTERACTIVELY once: run 'claude' and complete
       the OAuth flow. Workers run on the subscription, not an API key — an
       unauthenticated CLI makes every dispatch fail at spawn.")

# ── 5. Clone the repos ──────────────────────────────────────────────────────
step "5. Repo clones"

mkdir -p "$SRC_DIR"
if [[ "$DO_CLONES" == "1" ]]; then
  for repo in "${REPOS[@]}"; do
    if [[ -d "$SRC_DIR/$repo/.git" ]]; then
      skip "$repo already cloned"
    else
      gh repo clone "JDonaghy/$repo" "$SRC_DIR/$repo" >/dev/null 2>&1 \
        && ok "cloned $repo ($(git -C "$SRC_DIR/$repo" rev-parse --abbrev-ref HEAD))" \
        || warn "clone FAILED: $repo"
    fi
  done
  echo "  NOTE: ~/src/<repo> is the worker WORKTREE BASE, not a convenience"
  echo "        checkout. Never delete one to 'fix' drift."
else
  skip "skipped (--skip-clones)"
fi

# ── 6. coord venv ───────────────────────────────────────────────────────────
step "6. coord agent venv"

# INVARIANT (#402/#2569): ~/.coord-venv is a plain, NON-editable PyPI install.
# The [server] extra is mandatory (#1237) — the base package is a client-only
# CLI with no starlette/uvicorn and `coord agent` refuses to boot without it.
if [[ -d "$VENV_DIR" && -x "$VENV_DIR/bin/pip" ]]; then
  skip "existing venv at $VENV_DIR — upgrading in place"
elif [[ -d "$VENV_DIR" ]]; then
  die "$VENV_DIR exists but has no bin/pip (partial install). DELETE it and re-run
       — a partial venv poisons every retry (#2915)."
else
  "$PYTHON312" -m venv "$VENV_DIR"
  ok "created $VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install --upgrade 'code-coordinator[server]' -q
ok "installed: $("$VENV_DIR/bin/coord" version 2>/dev/null || echo '??')"

# ── 7. coord CLI shim (#2936) ───────────────────────────────────────────────
step "7. coord CLI shim"

# Workers are spawned with the agent's venv stripped from PATH (#402), so
# nothing otherwise guarantees a worker can resolve `coord` to record a test
# verdict. ~/.local/bin is on the agent PATH and is NOT stripped.
# Must point at $VENV_DIR itself, never a readlink -f'd blue/green path.
mkdir -p "$SHIM_DIR"
if [[ -L "$SHIM_DIR/coord" && "$(readlink "$SHIM_DIR/coord")" == "$VENV_DIR/bin/coord" ]]; then
  skip "coord shim already correct"
elif [[ -e "$SHIM_DIR/coord" || -L "$SHIM_DIR/coord" ]]; then
  warn "$SHIM_DIR/coord exists and is not the coord shim — left as-is"
else
  ln -s "$VENV_DIR/bin/coord" "$SHIM_DIR/coord"
  ok "coord shim -> $VENV_DIR/bin/coord"
fi

# ── 8. Node shims (#1678) ───────────────────────────────────────────────────
step "8. Node shims"

NODE_SHIM_SRC="$SRC_DIR/code-coordinator/deploy/node-shim.sh"
if [[ -f "$NODE_SHIM_SRC" ]]; then
  install -m 0755 "$NODE_SHIM_SRC" "$SHIM_DIR/coord-node-shim"
  ok "installed coord-node-shim from the checkout"
  for n in node npm npx; do
    if [[ -L "$SHIM_DIR/$n" && "$(readlink "$SHIM_DIR/$n")" == "$SHIM_DIR/coord-node-shim" ]]; then
      skip "$n shim already correct"
    elif [[ -e "$SHIM_DIR/$n" || -L "$SHIM_DIR/$n" ]]; then
      warn "$SHIM_DIR/$n exists and is not the coord node shim — left as-is"
    else
      ln -s "$SHIM_DIR/coord-node-shim" "$SHIM_DIR/$n"
      ok "$n shim installed"
    fi
  done
else
  warn "no $NODE_SHIM_SRC — clone code-coordinator first, then re-run"
fi

# ── 9. coordinator.yml symlink ──────────────────────────────────────────────
step "9. coordinator.yml"

# An agent that comes up config-free publishes NO capabilities at all, and
# every config-vs-/health cross-check then reads as absence rather than truth.
# This must exist BEFORE the agent starts.
mkdir -p "$COORD_DIR"
# ~/.coord as an alias of the native dir: every runbook, and this script's own
# plist log paths, spell it that way. One directory, two names — never two
# directories, which is how the agent and the interactive CLI end up
# disagreeing about where state lives.
if [[ ! -e "$COORD_COMPAT_LINK" ]]; then
  ln -s "$COORD_DIR" "$COORD_COMPAT_LINK" && ok "~/.coord -> $COORD_DIR"
elif [[ -L "$COORD_COMPAT_LINK" ]]; then
  skip "~/.coord already a symlink -> $(readlink "$COORD_COMPAT_LINK")"
else
  warn "~/.coord is a real directory but coord reads $COORD_DIR on macOS.
       Move its contents and replace it with a symlink, or the two will drift."
fi
TRACKED_YML="$SETTINGS_DIR/coord/coordinator.yml"
if [[ ! -f "$TRACKED_YML" ]]; then
  warn "no $TRACKED_YML — coord-settings did not clone; agent will start config-free"
elif [[ -L "$COORD_DIR/coordinator.yml" ]]; then
  skip "already a symlink -> $(readlink "$COORD_DIR/coordinator.yml")"
elif [[ -e "$COORD_DIR/coordinator.yml" ]]; then
  warn "$COORD_DIR/coordinator.yml is a REGULAR FILE, not a symlink. A pushed
       config edit will have no effect here and nothing says so (#2915).
       Replace it by hand once you have checked what is in it."
else
  ln -s "$TRACKED_YML" "$COORD_DIR/coordinator.yml"
  ok "coordinator.yml -> $TRACKED_YML"
fi

# ── 10. graphify ────────────────────────────────────────────────────────────
step "10. graphify"

# Package is "graphifyy" (double y); the binary is "graphify". Without the CLI
# every graph query on this machine degrades to grep SILENTLY.
if command -v graphify >/dev/null 2>&1 || [[ -x "$HOME/.local/bin/graphify" ]]; then
  skip "graphify already installed"
else
  pipx install graphifyy && ok "installed graphifyy" || warn "pipx install graphifyy failed"
fi
if [[ -x "$HOME/.local/bin/graphify" ]]; then
  "$HOME/.local/bin/graphify" claude install >/dev/null 2>&1 \
    && ok "graphify claude install" \
    || warn "graphify claude install failed"
  for repo in "${REPOS[@]}"; do
    [[ -d "$SRC_DIR/$repo/.git" ]] || continue
    ( cd "$SRC_DIR/$repo" && "$HOME/.local/bin/graphify" hook install >/dev/null 2>&1 ) \
      && ok "hooks: $repo" || warn "graphify hook install failed in $repo"
  done
fi
# Deliberately NOT setting core.hooksPath: it is unset fleet-wide as the #1617
# mitigation, and it REPLACES .git/hooks wholesale. Leave it alone.

# ── 11. Agent smoke ─────────────────────────────────────────────────────────
step "11. Agent smoke test (foreground, 5s)"

AGENT_PATH="$VENV_DIR/bin:$HOME/.cargo/bin:$HOME/.local/bin:$BREW_PREFIX/bin:/usr/local/bin:/usr/bin:/bin"
echo "  agent PATH will be: $AGENT_PATH"
for tool in cargo node claude git; do
  if PATH="$AGENT_PATH" command -v "$tool" >/dev/null 2>&1; then
    ok "$tool resolves on the agent PATH"
  else
    warn "$tool does NOT resolve on the agent PATH — its capability will probe
       'not found' and smoke dispatch will quietly refuse to route (#1671)."
  fi
done

# ── 12. launchd plist (interim — #1158 / CP-3 is unbuilt) ───────────────────
step "12. launchd plist"

PLIST="$HOME/Library/LaunchAgents/com.jdonaghy.coord-agent.plist"
if [[ "$DO_LAUNCHD" == "1" ]]; then
  mkdir -p "$HOME/Library/LaunchAgents" "$COORD_DIR/logs"
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>com.jdonaghy.coord-agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV_DIR/bin/coord</string>
    <string>agent</string>
    <string>--machine</string>
    <string>$MACHINE_NAME</string>
    <string>--port</string>
    <string>$AGENT_PORT</string>
  </array>
  <!-- #1671: this PATH is the whole ballgame. A launchd job's default PATH
       reaches neither ~/.cargo/bin nor Homebrew's prefix, and a capability
       that probes 'not found' fails SILENTLY (agent up, /health answering,
       dispatch refused in a 30s retry loop with no board-visible reason). -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>  <string>$AGENT_PATH</string>
    <key>HOME</key>  <string>$HOME</string>
  </dict>
  <!-- KeepAlive mirrors the Linux unit's Restart=always (#2938): POST /update
       stops this process cleanly so it can return on a swapped venv, and
       nothing else would bring it back.
       NOTE the difference from systemd: ThrottleInterval only RATE-LIMITS the
       respawn (one per 10s). It has no equivalent of StartLimitBurst, so a
       genuinely broken binary retries forever instead of landing in `failed`.
       Check `launchctl print gui/$UID/com.jdonaghy.coord-agent` for a
       non-zero "last exit code" — a crash loop here is quiet. -->
  <key>KeepAlive</key>          <true/>
  <key>RunAtLoad</key>          <true/>
  <key>ThrottleInterval</key>   <integer>10</integer>
  <key>StandardOutPath</key>    <string>$COORD_DIR/logs/coord-agent.out.log</string>
  <key>StandardErrorPath</key>  <string>$COORD_DIR/logs/coord-agent.err.log</string>
</dict>
</plist>
PLIST_EOF
  ok "wrote $PLIST (NOT loaded — see below)"
  RESIDUE+=("The plist is hand-rolled and INTERIM. deploy/ has no checked-in
       launchd unit; that is issue #1158 (CP-3), still open. Load it with:
         launchctl bootstrap gui/\$(id -u) $PLIST
       and verify with 'coord machine doctor $MACHINE_NAME' from the
       coordinator — not just by seeing the process alive.")
  RESIDUE+=("launchd LaunchAgents start at LOGIN, not at boot. Enable auto-login
       (System Settings > Users & Groups) or the agent will not come back after
       a reboot. This is also what GUI/GTK smoke work needs.")
else
  skip "skipped (pass --with-launchd to write the interim plist)"
  echo "  Until then, run the agent in tmux:"
  echo "    tmux new -d -s coord-agent 'PATH=$AGENT_PATH $VENV_DIR/bin/coord agent --machine $MACHINE_NAME --port $AGENT_PORT'"
fi

# ── Done ────────────────────────────────────────────────────────────────────
step "Done — residue"

# bash 3.2 (what macOS ships) errors on an empty array under `set -u`, so
# every expansion of RESIDUE is guarded with the ${arr[@]+...} idiom.
if [[ -z "${RESIDUE[*]+x}" ]]; then
  echo "  (nothing outstanding)"
else
  i=1
  for item in ${RESIDUE[@]+"${RESIDUE[@]}"}; do
    printf '  %d. %s\n' "$i" "$item"
    i=$((i+1))
  done
fi

cat <<'NEXT_EOF'

Still to do from the COORDINATOR (not this box):
  1. coord machine add macmini --host <the MagicDNS name printed above> ...
     then commit + push in coord-settings and `git pull` on dellserver.
  2. coord machine doctor macmini --ssh   (needs Remote Login on)
  3. Run the claude-coordinator test suite by hand here before letting
     dispatch route work to it — the coord pytest suite has never been run
     on macOS, and tests are where environment leaks in.
NEXT_EOF
