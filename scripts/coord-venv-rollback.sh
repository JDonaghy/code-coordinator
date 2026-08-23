#!/usr/bin/env bash
#
# coord-venv-rollback.sh — one-command last-known-good rollback for
# ~/.coord-venv, the blue/green venv `coord agent update` swaps (#1241,
# coord/agent_update.py). Mirrors deploy/coord-web-rollback.sh (#1560) — same
# shape, different swap target.
#
# What it does: repoints ~/.coord-venv (a symlink onto either ~/.coord-venv.
# blue or ~/.coord-venv.green) at whichever sibling slot is currently
# healthy, if the slot it currently resolves to is not. No coord-agent (or
# coord-serve/coord-web/coord-drive-queue/coord-notify — anything execing
# from this venv) restart is required for the symlink flip itself, but a
# live process keeps running its already-open interpreter until it IS
# restarted — see the note this script prints at the end.
#
# Why bash, not Python: this is the thing run by hand at 3am, and
# scripts/fleet_watchdog.py (#2580) shells out to it as its Tier-1 repair
# for a broken venv precisely BECAUSE it must not itself depend on ``coord``
# being importable (the failure mode this exists to fix is "coord is not
# importable"). A handful of readable bash lines with no interpreter story
# is the whole point — see docs/AGENT_OPERATIONS.md's INVARIANT section for
# the outage this automates (2026-08-22, #2569/#2570/#2572).
#
# Root incident this fixes: ~/.coord-venv became an EDITABLE install
# pointing at a worker worktree that was later reaped, so every subsequent
# `import coord` in that slot failed. The recovery was: flip the symlink
# back onto the other (PyPI, healthy) blue/green slot. Nothing about that
# needs coord itself to run.
#
# Health check for a slot: does `<slot>/bin/python3 -c "import coord.state,
# coord.commands.review"` succeed (the same two modules
# coord.agent_update._smoke_check imports), is the install NOT editable
# (`<slot>/bin/pip show code-coordinator` has no "Editable project
# location:" line — the exact detection command documented in
# docs/AGENT_OPERATIONS.md's INVARIANT section), and does `<slot>/bin/coord
# --version` exit 0. All three must pass for a slot to count as healthy.
#
# Refuses (exit 1, no mutation) rather than guessing when: ~/.coord-venv
# isn't a blue/green symlink at all (nothing to roll back to), the sibling
# slot doesn't exist, or the sibling slot is ALSO broken. Also a clean no-op
# (exit 0, no mutation) when the current slot is already healthy — running
# this by hand when nothing is actually wrong must be harmless.
#
# Usage:
#   ~/.local/bin/coord-venv-rollback.sh
#   VENV_DIR=/custom/path ~/.local/bin/coord-venv-rollback.sh   # tests / non-default installs
#
# Install (one-time, alongside coord-venv-rollback.sh's caller,
# scripts/fleet_watchdog.py):
#   cp scripts/coord-venv-rollback.sh ~/.local/bin/
#   chmod +x ~/.local/bin/coord-venv-rollback.sh

set -uo pipefail

# Absolute paths for every binary this script shells out to (fleet_watchdog.py
# constraint 4, #2580) — a cron/systemd PATH is barer than a login shell's,
# and a rollback script that silently resolves the wrong `mv` is worse than
# one that refuses. Mirrors fleet_watchdog.py's `_first_existing`.
resolve_bin() {
  local candidate
  for candidate in "$@"; do
    if [[ -x "$candidate" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

DATE_BIN="$(resolve_bin /usr/bin/date /bin/date)" || {
  echo "ERROR: no 'date' binary at any known absolute path — refusing to guess via PATH." >&2
  exit 1
}
READLINK_BIN="$(resolve_bin /usr/bin/readlink /bin/readlink)" || {
  echo "ERROR: no 'readlink' binary at any known absolute path — refusing to guess via PATH." >&2
  exit 1
}
GREP_BIN="$(resolve_bin /usr/bin/grep /bin/grep)" || {
  echo "ERROR: no 'grep' binary at any known absolute path — refusing to guess via PATH." >&2
  exit 1
}
LN_BIN="$(resolve_bin /usr/bin/ln /bin/ln)" || {
  echo "ERROR: no 'ln' binary at any known absolute path — refusing to guess via PATH." >&2
  exit 1
}
MV_BIN="$(resolve_bin /usr/bin/mv /bin/mv)" || {
  echo "ERROR: no 'mv' binary at any known absolute path — refusing to guess via PATH." >&2
  exit 1
}

# `readlink -f` is a GNU coreutils extension — the BSD `readlink` shipped on
# macOS (the Mac mini agent host, docs/MAC_MINI.md) has no `-f` at all. Check
# up front and fail with a clear message rather than let a silent option
# error downstream masquerade as "not a recognized .blue/.green suffix".
if ! "$READLINK_BIN" -f / >/dev/null 2>&1; then
  echo "ERROR: $READLINK_BIN does not support '-f' (looks like a non-GNU readlink, e.g. BSD/macOS) — this script needs GNU coreutils." >&2
  exit 1
fi

VENV_DIR="${VENV_DIR:-$HOME/.coord-venv}"

say() { echo "[$("$DATE_BIN" -Is)] $*" >&2; }

# Empty stdout => healthy. Non-empty stdout => the reason it's broken.
# Never trips `set -e` (there isn't one) on a missing/failing subprocess —
# a broken slot is exactly the expected input here, not a script bug.
slot_broken_reason() {
  local slot="$1"
  local py="$slot/bin/python3"
  local pip="$slot/bin/pip"
  local coord_bin="$slot/bin/coord"

  if [[ ! -x "$py" ]]; then
    echo "no python3 at $py"
    return
  fi
  if ! "$py" -c 'import coord.state, coord.commands.review' >/dev/null 2>&1; then
    echo "coord is not importable from $slot"
    return
  fi
  if [[ -x "$pip" ]] && "$pip" show code-coordinator 2>/dev/null | "$GREP_BIN" -qi '^Editable project location:'; then
    echo "editable install at $slot"
    return
  fi
  if [[ ! -x "$coord_bin" ]] || ! "$coord_bin" --version >/dev/null 2>&1; then
    echo "coord --version fails at $slot"
    return
  fi
  echo -n ""
}

if [[ ! -L "$VENV_DIR" ]]; then
  say "ERROR: $VENV_DIR is not a symlink — not migrated to the blue/green layout (or doesn't exist)."
  say "Nothing this script can roll back to; see coord.agent_update.ensure_symlink_layout for the one-time migration."
  exit 1
fi

CURRENT="$("$READLINK_BIN" -f "$VENV_DIR")"
CURRENT="${CURRENT%/}"

case "$CURRENT" in
  *.blue) SIBLING="${CURRENT%.blue}.green" ;;
  *.green) SIBLING="${CURRENT%.green}.blue" ;;
  *)
    say "ERROR: $VENV_DIR resolves to $CURRENT, which has no recognized .blue/.green suffix — refusing to guess a sibling."
    exit 1
    ;;
esac

CURRENT_REASON="$(slot_broken_reason "$CURRENT")"
if [[ -z "$CURRENT_REASON" ]]; then
  say "OK: $VENV_DIR (-> $CURRENT) is already healthy — nothing to roll back."
  exit 0
fi
say "current slot is broken: $CURRENT ($CURRENT_REASON)"

if [[ ! -d "$SIBLING" ]]; then
  say "ERROR: sibling slot $SIBLING does not exist — refusing to roll back onto nothing."
  exit 1
fi

SIBLING_REASON="$(slot_broken_reason "$SIBLING")"
if [[ -n "$SIBLING_REASON" ]]; then
  say "ERROR: sibling slot $SIBLING is ALSO broken ($SIBLING_REASON) — refusing to roll back onto a broken slot."
  exit 1
fi

say "sibling slot is healthy: $SIBLING — rolling back: $CURRENT -> $SIBLING"

# Atomic publish — identical pattern to coord-web-rollback.sh / coord.
# agent_update._atomic_swap: symlink under a temp name, then rename(2) over
# the live name. No window where $VENV_DIR is missing or half-updated.
"$LN_BIN" -sfn "$SIBLING" "$VENV_DIR.new"
"$MV_BIN" -Tf "$VENV_DIR.new" "$VENV_DIR"

say "done: $VENV_DIR -> $SIBLING"
say "Any process already running out of $CURRENT (coord-agent, coord-serve, coord-web, coord-drive-queue.service, coord-notify.service, ...) keeps executing its already-open interpreter from the OLD slot until it restarts — the symlink flip alone does not evict it. Restart the affected unit(s):"
say "  XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user restart coord-agent"
