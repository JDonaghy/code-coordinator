#!/usr/bin/env bash
# driver.sh — tmux-based driver for coord-tui
#
# Run from the repo root:
#   .claude/skills/run-claude-coordinator/driver.sh launch
#   .claude/skills/run-claude-coordinator/driver.sh screen
#   .claude/skills/run-claude-coordinator/driver.sh key <key>
#   .claude/skills/run-claude-coordinator/driver.sh view <n>     # 1=Board 2=Machines 3=Pipeline 4=Settings 5=Terminal 6=Kanban
#   .claude/skills/run-claude-coordinator/driver.sh wait <text> [secs]
#   .claude/skills/run-claude-coordinator/driver.sh quit
#   .claude/skills/run-claude-coordinator/driver.sh kill
#
# IMPORTANT: always call with an absolute path to the binary — tmux
# launches commands with HOME as the CWD, so relative paths break.

set -euo pipefail

SESSION=coord-tui-driver
# Resolve the binary path to an absolute path at startup.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
TUI_BIN="$REPO_ROOT/tui/target/debug/coord-tui"

cmd="${1:-}"

case "$cmd" in
  launch)
    if [ ! -x "$TUI_BIN" ]; then
      echo "ERROR: $TUI_BIN not found or not executable." >&2
      echo "Build first:  cd tui && cargo build" >&2
      exit 1
    fi
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    tmux new-session -d -s "$SESSION" -x 160 -y 48 "$TUI_BIN"
    # Wait for the initial chrome render (BOARD header appears immediately)
    timeout 10 bash -c \
      "until tmux capture-pane -t $SESSION -p | grep -q 'BOARD'; do sleep 0.2; done" \
      || { echo "ERROR: TUI did not render within 10s. Check $HOME/.coord/coord-tui-panic.log" >&2; exit 1; }
    # Wait for the data-load spinner to clear (loads from ~/.coord/coord.db)
    timeout 15 bash -c \
      "until ! tmux capture-pane -t $SESSION -p | grep -q 'loading'; do sleep 0.3; done" || true
    echo "coord-tui started (session: $SESSION, binary: $TUI_BIN)"
    ;;

  screen)
    tmux capture-pane -t "$SESSION" -p
    ;;

  key)
    key="${2:?Usage: driver.sh key <key>}"
    tmux send-keys -t "$SESSION" "$key"
    ;;

  view)
    n="${2:?Usage: driver.sh view <1-6>}"
    tmux send-keys -t "$SESSION" "$n"
    sleep 0.4
    tmux capture-pane -t "$SESSION" -p
    ;;

  wait)
    text="${2:?Usage: driver.sh wait <text> [seconds]}"
    secs="${3:-10}"
    timeout "$secs" bash -c \
      "until tmux capture-pane -t $SESSION -p | grep -qF $(printf '%q' "$text"); do sleep 0.2; done" \
      || { echo "ERROR: '$text' did not appear within ${secs}s" >&2; tmux capture-pane -t "$SESSION" -p >&2; exit 1; }
    echo "found: $text"
    ;;

  quit)
    tmux send-keys -t "$SESSION" 'q' 2>/dev/null || true
    sleep 0.5
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    echo "stopped"
    ;;

  kill)
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    echo "killed"
    ;;

  *)
    echo "Usage: $0 {launch|screen|key <k>|view <n>|wait <text> [s]|quit|kill}" >&2
    exit 1
    ;;
esac
