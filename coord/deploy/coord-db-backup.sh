#!/usr/bin/env bash
# Interim coord.db snapshot to the external SSD (pending #1822).
#
# VACUUM INTO, not cp: it takes a consistent snapshot of a live SQLite database
# while coord-serve keeps writing. A plain cp of a WAL-mode db under load can
# capture a torn file, which is the failure mode you only discover at restore.
#
# Every snapshot is integrity-checked and sanity-checked (assignments table
# present and non-empty) before it is allowed to count, and any failed check
# leaves the snapshot on disk named .REJECTED so it can be looked at rather
# than silently deleted OR silently left lying around under its normal name,
# indistinguishable from a good backup.
#
# NOT a substitute for off-box backup: this protects against db corruption, a
# bad migration, accidental deletion and OS-disk failure. It does NOT protect
# against the machine being lost, stolen or burned. See #1822.
#
# #3085: this lane protects a live SQLite coord.db, full stop. After a
# Postgres cutover (#829) the SQLite file, if it is even still there, is a
# frozen, dead snapshot of pre-cutover state -- every guard below (mountpoint,
# VACUUM INTO, integrity_check, the assignments-non-empty check) would pass
# against it happily and print a green "ok" line for a backup that would roll
# the fleet back to before the cutover. So the very first thing this script
# does is ask `coord` which backend is actually configured (`coord
# store-backend`, a thin wrapper over coord.db.resolve_store_backend() --
# #3084/#3085) rather than re-parsing coordinator.yml's `store:` block here in
# bash, and refuses to run at all, before touching the filesystem, when the
# answer isn't sqlite. If `coord` itself can't answer (not on PATH, or exits
# nonzero for any reason -- including a malformed `store:` block, which
# resolve_store_backend() deliberately raises on), this also refuses rather
# than falling back to assuming sqlite.
set -uo pipefail

SRC="${COORD_DB:-$HOME/.coord/coord.db}"
DEST_DIR="${COORD_BACKUP_DIR:-/media/crucial/coord-backups}"
RETAIN="${COORD_BACKUP_RETAIN:-168}"        # hourly x 7 days
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DEST_DIR/coord.db.$STAMP"

fail() { echo "coord-db-backup: FAILED: $*" >&2; exit 1; }

COORD_BIN="${COORD_BIN:-coord}"
command -v "$COORD_BIN" >/dev/null 2>&1 \
  || fail "cannot determine the store backend: '$COORD_BIN' not found on \$PATH — refusing to assume sqlite (see #1822)"

# stderr can carry unrelated dev-only warnings (e.g. an editable-install
# worktree not on its default branch) that must not contaminate the one
# line of stdout we parse below, so the two streams are kept separate
# rather than merged — but both are captured in a single invocation
# (re-running a second time on failure would risk the two calls observing
# a config file mid-edit differently, and is an avoidable extra subprocess
# spawn besides). BACKEND_ERR is only used in the fail() message below.
BACKEND_ERR_FILE="$(mktemp)"
trap 'rm -f "$BACKEND_ERR_FILE"' EXIT
BACKEND_OUTPUT="$("$COORD_BIN" store-backend 2>"$BACKEND_ERR_FILE")"
BACKEND_RC=$?
if [ "$BACKEND_RC" -ne 0 ]; then
  BACKEND_ERR="$(cat "$BACKEND_ERR_FILE")"
  fail "cannot determine the store backend: '$COORD_BIN store-backend' exited $BACKEND_RC — refusing to assume sqlite (see #1822): ${BACKEND_ERR:-<no output>}"
fi

# stdout is "<backend>" or "<backend> <redacted target>" — first word only.
BACKEND="$(printf '%s\n' "$BACKEND_OUTPUT" | awk 'NF { print $1; exit }')"
[ -n "$BACKEND" ] \
  || fail "cannot determine the store backend: '$COORD_BIN store-backend' printed nothing — refusing to assume sqlite (see #1822)"

if [ "$BACKEND" != "sqlite" ]; then
  fail "the configured store backend is '$BACKEND', not sqlite — this lane only protects the SQLite file and does not know how to back up $BACKEND. No snapshot written. The real, backend-agnostic answer is #1822 (continuous backup + verified restore), which this issue does not replace."
fi

[ -f "$SRC" ] || fail "source db not found: $SRC"

# The mount must actually be a mount. If the SSD is unplugged, /media/crucial
# is still a directory on the root filesystem, and we would cheerfully write
# "backups" onto the very disk we are protecting against.
mountpoint -q "$(dirname "$DEST_DIR")" || fail "$(dirname "$DEST_DIR") is not a mountpoint — external SSD not mounted"

mkdir -p "$DEST_DIR" || fail "cannot create $DEST_DIR"

sqlite3 "$SRC" "VACUUM INTO '$OUT';" || fail "VACUUM INTO failed"

CHECK="$(sqlite3 "$OUT" 'PRAGMA integrity_check;' 2>&1)"
if [ "$CHECK" != "ok" ]; then
  mv "$OUT" "$OUT.REJECTED"
  fail "integrity_check on snapshot: $CHECK (kept as $OUT.REJECTED)"
fi

# Prove it is a coord db and not an empty file that passed integrity_check.
# Same "keep it as .REJECTED, never silently delete or silently leave it
# lying around under the normal name" contract as the integrity_check
# rejection above -- an unlinked, unmarked snapshot sitting next to good
# ones is exactly the kind of thing retention pruning would eventually
# rotate out indistinguishably from a real backup.
ROWS="$(sqlite3 "$OUT" 'SELECT COUNT(*) FROM assignments;' 2>&1)"
RC=$?
if [ "$RC" -ne 0 ]; then
  mv "$OUT" "$OUT.REJECTED"
  fail "snapshot has no assignments table: $ROWS (kept as $OUT.REJECTED)"
fi
case "$ROWS" in
  ''|*[!0-9]*)
    mv "$OUT" "$OUT.REJECTED"
    fail "unexpected assignments count: $ROWS (kept as $OUT.REJECTED)"
    ;;
esac
if [ "$ROWS" -eq 0 ]; then
  mv "$OUT" "$OUT.REJECTED"
  fail "snapshot has 0 assignments — refusing to count this as a backup (kept as $OUT.REJECTED)"
fi

ln -sfn "$OUT" "$DEST_DIR/coord.db.latest"

# Prune oldest beyond RETAIN. Never touches .REJECTED files.
mapfile -t OLD < <(ls -1 "$DEST_DIR"/coord.db.2* 2>/dev/null | grep -v '\.REJECTED$' | sort | head -n -"$RETAIN")
for f in "${OLD[@]:-}"; do [ -n "$f" ] && rm -f "$f"; done

SIZE="$(du -h "$OUT" | cut -f1)"
echo "coord-db-backup: ok $OUT ($SIZE, ${ROWS} assignments, $(ls -1 "$DEST_DIR"/coord.db.2* 2>/dev/null | grep -vc '\.REJECTED$') snapshots retained)"
