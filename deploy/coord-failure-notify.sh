#!/usr/bin/env bash
#
# coord-failure-notify.sh — last-resort, coord-INDEPENDENT escalation for a
# failed coord daemon systemd unit (#2572).
#
# Invoked via `OnFailure=coord-failure-notify.service` from
# coord-drive-queue.service / coord-notify.service (see those units' [Unit]
# sections) — and any future daemon unit that adds the same line. This
# script is deliberately as dumb as possible: no `coord` import, no Python,
# no read of coordinator.yml, no dependency on `~/.coord-venv` or any git
# checkout being present or even importable.
#
# That is the whole point. #2569/#2570 (2026-08-22): an editable
# `~/.coord-venv` pointed at a worker's worktree, and when that worktree was
# later deleted, EVERY unit exec'ing `~/.coord-venv/bin/coord` — including
# coord-drive-queue.service and coord-notify.service — crash-looped for
# ~11h with zero operator-visible signal. `coord health`'s own `agent_venv`
# check had already gone CRIT on the editable venv from the first minute,
# and coord-drive-queue.service's own tick correctly self-cordoned and said
# so in its logs — but the ONLY channel that turns either of those into a
# phone push (`coord notifier`, #1632) runs from that exact same broken
# venv, so nothing ever reached a human (#2572). This script's entire
# reason to exist is to still work when `coord`, Python, and every git
# checkout on the box are unavailable — its only real dependencies are
# `systemctl`, `logger`, `wall`, `curl` and (optionally) network reachability
# to an ntfy server, none of which live inside this repo or its venvs.
#
# Channels tried, in order, ALL best-effort (a failure in one must not skip
# the rest, and this script must never itself exit non-zero — it is the
# last-resort channel, so nothing it does may end up "failed" and silently
# drop the alert):
#
#   1. `logger` — always, unconditionally. Lands in the systemd journal so
#      `journalctl --user -t coord-failure-notify` finds it even when
#      nothing else here is configured or reachable.
#   2. `wall`   — best-effort, in case an operator happens to have a
#      terminal open on the box right now. Free, zero config, often useless
#      (headless box, 4am) but never harmful.
#   3. ntfy push — the actual phone-reachable channel: the SAME self-hosted
#      ntfy server `coord notifier` already pushes to (docs/NOTIFIER.md),
#      configured here via a PLAIN KEY=VALUE env file (see
#      coord-failure-notify.service's EnvironmentFile= line) so reading it
#      costs nothing more than `.` sourcing a shell file — no YAML parser,
#      no Python, nothing that could itself be broken by the exact class of
#      outage this script exists to escalate.
#
# The ntfy push is debounced (default 30 min,
# $COORD_FAILURE_NOTIFY_MIN_INTERVAL_SEC) against a marker file, so a unit
# that keeps re-entering `failed` every few minutes (coord-drive-queue.timer
# fires every 3m) doesn't turn into a push every few minutes — logger/wall
# still fire every single time (cheap, useful for forensics/journal
# correlation), only the ntfy push is rate-limited.
set -uo pipefail

HOST="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown-host)"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown-time)"

FAILED="$(systemctl --user --failed --no-legend --plain 2>/dev/null \
  | awk '{print $1}' | paste -sd, - 2>/dev/null)"
if [ -z "$FAILED" ]; then
  FAILED="(systemctl --user --failed reported nothing at the moment this ran)"
fi

MSG="coord daemon unit failure on ${HOST} at ${NOW} — failed unit(s): ${FAILED}"

# 1. journal — unconditional, cheapest, never skipped.
logger -t coord-failure-notify -- "$MSG" 2>/dev/null || true

# 2. wall — best-effort, ignored if nobody is logged in or wall is missing.
if command -v wall >/dev/null 2>&1; then
  echo "$MSG" | wall 2>/dev/null || true
fi

# 3. ntfy push — debounced, and only when configured.
MIN_INTERVAL="${COORD_FAILURE_NOTIFY_MIN_INTERVAL_SEC:-1800}"
STATE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/coord-failure-notify"
STATE_FILE="$STATE_DIR/last-sent"
mkdir -p "$STATE_DIR" 2>/dev/null || true

SEND=1
if [ -f "$STATE_FILE" ]; then
  LAST="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
  case "$LAST" in
    ''|*[!0-9]*) LAST=0 ;;
  esac
  NOW_EPOCH="$(date -u +%s 2>/dev/null || echo 0)"
  AGE=$((NOW_EPOCH - LAST))
  if [ "$AGE" -ge 0 ] && [ "$AGE" -lt "$MIN_INTERVAL" ]; then
    SEND=0
  fi
fi

if [ "$SEND" -eq 1 ] && [ -n "${COORD_FAILURE_NTFY_URL:-}" ] && [ -n "${COORD_FAILURE_NTFY_TOPIC:-}" ]; then
  if command -v curl >/dev/null 2>&1; then
    HEADERS=(-H "Title: coord daemon unit failed on ${HOST}" -H "Priority: 5" -H "Tags: rotating_light")
    if [ -n "${COORD_FAILURE_NTFY_TOKEN:-}" ]; then
      HEADERS+=(-H "Authorization: Bearer ${COORD_FAILURE_NTFY_TOKEN}")
    fi
    if curl -fsS -m 10 "${HEADERS[@]}" -d "$MSG" \
        "${COORD_FAILURE_NTFY_URL%/}/${COORD_FAILURE_NTFY_TOPIC}" >/dev/null 2>&1; then
      date -u +%s > "$STATE_FILE" 2>/dev/null || true
    else
      logger -t coord-failure-notify -- "ntfy push failed (curl reported an error) — see the logger line above for the message it would have carried" 2>/dev/null || true
    fi
  else
    logger -t coord-failure-notify -- "ntfy configured (COORD_FAILURE_NTFY_URL/TOPIC set) but curl is not installed — cannot push" 2>/dev/null || true
  fi
fi

exit 0
