#!/usr/bin/env bash
#
# extract-coord-tui.sh — build the `coord-tui` repo from code-coordinator's
# `tui/` subdirectory, WITH history.  #2899, Phase 4 of #2894.
#
#   scripts/extract-coord-tui.sh [--source URL] [--ref REF] [--out DIR]
#                                [--from-commit SHA] [--keep]
#
# Produces a ready-to-push git repository in a scratch directory and PRINTS
# the two commands a human must run to publish it.  It never talks to GitHub
# and never pushes: creating the remote repo needs `gh`, which is on the
# worker deny-list (CLAUDE.md, "Rules for workers") — the coordinator owns
# every GitHub interaction.  Run this, eyeball the verification output, then
# run the two printed commands.
#
# WHY THIS IS A SCRIPT AND NOT A RUNBOOK.  The move is a one-shot, but it is
# a one-shot that is easy to get subtly wrong in a way nobody notices for
# months: a `--path-rename` that leaves a stray `tui/` prefix, a filter that
# drops the staged `.github/workflows/release-tui.yml`, or — the expensive
# one — a "history-preserving" extraction whose history does not actually
# survive `--follow`.  A script that ASSERTS those three things is worth more
# than prose that asks someone to check them.
#
# THE THREE THINGS IT VERIFIES BEFORE DECLARING SUCCESS:
#   1. `git log --follow src/app/data.rs` reaches commits that PREDATE the
#      extraction — the acceptance criterion, and the only real proof that
#      history came along rather than being flattened into one commit.
#   2. No path in the result still starts with `tui/`.
#   3. The crate root is the repo root: `Cargo.toml`, `src/main.rs`,
#      `tests/acceptance.rs` all exist at the top level.
#
# HOW THE SPLIT IS DONE.  `git filter-repo` when available (upstream's
# recommendation, and what the issue specifies); `git subtree split`
# otherwise.  The fallback is not a lesser result for THIS repo's shape —
# `tui/` was never renamed into or out of, so a subtree split of it produces
# the same commit graph — but it IS slower, and it cannot rewrite tags.  The
# script says which one it used, loudly, because "which tool ran" is the
# first question anyone debugging the result will ask.
set -euo pipefail

SOURCE_URL="https://github.com/JDonaghy/code-coordinator.git"
SOURCE_REF="main"
OUT_DIR=""
FROM_COMMIT=""
KEEP=0
SUBDIR="tui"
SCAFFOLD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/coord-tui-scaffold"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)       SOURCE_URL="$2"; shift 2 ;;
        --ref)          SOURCE_REF="$2"; shift 2 ;;
        --out)          OUT_DIR="$2"; shift 2 ;;
        # The commit to extract FROM. Defaults to the last commit that still
        # had `tui/` — normally the parent of the commit that deleted it, so
        # this script keeps working after the deletion has landed on main.
        --from-commit)  FROM_COMMIT="$2"; shift 2 ;;
        --keep)         KEEP=1; shift ;;
        -h|--help)      sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

say() { printf '[extract] %s\n' "$*"; }
die() { printf '[extract] FATAL: %s\n' "$*" >&2; exit 1; }

if [[ -z "$OUT_DIR" ]]; then
    OUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/coord-tui-extract.XXXXXX")/coord-tui"
fi
[[ -e "$OUT_DIR" ]] && die "$OUT_DIR already exists — pass a fresh --out"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/coord-tui-clone.XXXXXX")"
cleanup() { [[ "$KEEP" -eq 1 ]] || rm -rf "$WORK"; }
trap cleanup EXIT

# A FRESH clone, never an existing checkout.  Both tools below REWRITE
# history in place; pointing either at a working checkout (or, worse, at a
# checkout other worktrees share) destroys it.  `--no-local` defeats git's
# hardlink optimisation for file:// sources for the same reason.
say "cloning $SOURCE_URL@$SOURCE_REF (fresh, throwaway)"
git clone --no-local --quiet "$SOURCE_URL" "$WORK/src" 2>/dev/null \
    || git clone --quiet "$SOURCE_URL" "$WORK/src"
cd "$WORK/src"
git checkout --quiet "$SOURCE_REF"

# `$SUBDIR/Cargo.toml`, not bare `-d "$SUBDIR"`: the crate's move is required
# to leave `tui/tests/acceptance.rs` + `tui/tests/acceptance/**` PHYSICALLY
# behind, unmoved — they are this repo's sealed acceptance oracle (#944), and
# a `type="work"` diff may never delete a sealed path (coord/review.py); see
# the review finding on #2899 this script's own comment above links to. So
# `tui/` keeps existing (non-empty) at the tip even after the crate itself is
# gone, and directory presence alone can no longer tell "has the move
# happened yet". `Cargo.toml` is the crate's own marker instead.
if [[ -z "$FROM_COMMIT" ]]; then
    if [[ -f "$SUBDIR/Cargo.toml" ]]; then
        FROM_COMMIT="$(git rev-parse HEAD)"
    else
        # The crate is already gone from the tip (orphaned sealed test files
        # under `$SUBDIR/tests/acceptance{.rs,/}` may still be sitting there —
        # expected, see above). Find the last commit that had `Cargo.toml`:
        # `git log -- <path>` still finds the DELETING commit, so take its
        # parent. Reported explicitly — silently extracting from an
        # unexpected commit is precisely how you get a repo whose history
        # looks fine and whose contents are a month stale.
        deleting="$(git log --format=%H --diff-filter=D -1 -- "$SUBDIR/Cargo.toml" || true)"
        [[ -n "$deleting" ]] || die "no $SUBDIR/Cargo.toml at $SOURCE_REF and no commit that deleted it"
        FROM_COMMIT="$(git rev-parse "$deleting^")"
        say "$SUBDIR/Cargo.toml is absent at $SOURCE_REF; extracting from $FROM_COMMIT (parent of the deleting commit $deleting)"
    fi
fi
git checkout --quiet "$FROM_COMMIT"
[[ -f "$SUBDIR/Cargo.toml" ]] || die "$SUBDIR/Cargo.toml does not exist at $FROM_COMMIT"
say "extracting from $(git rev-parse --short HEAD) ($(git log -1 --format=%s | cut -c1-60))"

# ── the split ───────────────────────────────────────────────────────────────
if command -v git-filter-repo >/dev/null 2>&1 || git filter-repo --help >/dev/null 2>&1; then
    say "TOOL: git filter-repo"
    git branch --quiet -f extract-tip HEAD
    git checkout --quiet extract-tip
    git filter-repo --force --quiet \
        --path "$SUBDIR/" --path-rename "$SUBDIR/:" --refs extract-tip
    mkdir -p "$(dirname "$OUT_DIR")"
    git clone --quiet --no-local --branch extract-tip . "$OUT_DIR"
    git -C "$OUT_DIR" branch --quiet -m extract-tip main
else
    say "TOOL: git subtree split (git-filter-repo not installed)"
    say "      Equivalent here — $SUBDIR/ was never renamed into or out of, so the"
    say "      commit graph is the same. Slower, and it cannot rewrite tags."
    # `git subtree split` writes a per-commit progress counter to stdout,
    # thousands of lines of it, which would bury every message this script
    # actually wants read. Keep only the final SHA it prints.
    split_log="$WORK/subtree-split.log"
    git subtree split --prefix="$SUBDIR" HEAD >"$split_log" 2>&1 || die "git subtree split failed; see $split_log"
    split_sha="$(tail -1 "$split_log" | tr -d '\r' | grep -oE '[0-9a-f]{40}' | tail -1)"
    [[ -n "$split_sha" ]] || die "git subtree split produced nothing"
    mkdir -p "$OUT_DIR"
    git init --quiet --initial-branch=main "$OUT_DIR"
    git -C "$OUT_DIR" fetch --quiet "$WORK/src" "$split_sha"
    git -C "$OUT_DIR" reset --hard --quiet FETCH_HEAD
    git -C "$OUT_DIR" branch --quiet -M main
fi

cd "$OUT_DIR"
git remote remove origin 2>/dev/null || true

# ── verification (the whole point) ──────────────────────────────────────────
fail=0
check() { if eval "$2"; then say "  OK   $1"; else say "  FAIL $1"; fail=1; fi; }

say "verifying the extraction"
check "crate root is the repo root (Cargo.toml)"      '[[ -f Cargo.toml ]]'
check "src/main.rs at the top level"                  '[[ -f src/main.rs ]]'
check "tests/acceptance.rs at the top level"          '[[ -f tests/acceptance.rs ]]'
check "the staged release workflow came along"        '[[ -f .github/workflows/release-tui.yml ]]'
check "no path still starts with $SUBDIR/"            '! git ls-files | grep -q "^$SUBDIR/"'

# THE acceptance criterion. `--follow` must reach commits older than the
# extraction, on a file the issue names specifically. `wc -l` rather than a
# fixed number: the exact count drifts with every future commit, but "more
# than a handful" is the thing being asserted and is stable.
follow_count="$(git log --follow --format=%H -- src/app/data.rs | wc -l | tr -d ' ')"
check "git log --follow src/app/data.rs shows real history ($follow_count commits, want >1)" \
      '[[ "$follow_count" -gt 1 ]]'

# ── scaffolding the new repo's own CI + rules ───────────────────────────────
if [[ -d "$SCAFFOLD" ]]; then
    say "copying scaffold from $SCAFFOLD (CI workflows, CLAUDE.md)"
    # Never clobber a file the history already carries — the staged
    # release-tui.yml arrived through the split and is the real one. `cp -n`
    # is non-portable (GNU coreutils warns that its behaviour may change), so
    # copy file-by-file and skip existing destinations explicitly.
    while IFS= read -r rel; do
        dest="$OUT_DIR/$rel"
        if [[ -e "$dest" ]]; then
            say "  skip (already in history): $rel"
            continue
        fi
        mkdir -p "$(dirname "$dest")"
        cp "$SCAFFOLD/$rel" "$dest"
    done < <(cd "$SCAFFOLD" && find . -type f -printf '%P\n' | sort)
    git add -A
    # A freshly `git init`ed repo inherits only the GLOBAL identity, and CI
    # boxes / fresh worker machines routinely have none — which turns this
    # into a confusing "Author identity unknown" fatal at the very last step,
    # after the expensive part already succeeded. Borrow the source clone's
    # identity, and fall back to an obviously-synthetic one rather than
    # failing: the commit is provenance, not authorship.
    git config user.name  "$(git -C "$WORK/src" config user.name  || echo 'coord extract-coord-tui')"
    git config user.email "$(git -C "$WORK/src" config user.email || echo 'extract-coord-tui@localhost')"
    git commit --quiet -m "#2899: coord-tui CI + worker rules for the standalone repo

Scaffolding that could not travel with the crate, because it did not exist
inside code-coordinator's tui/ subdirectory:

- .github/workflows/cargo-test.yml — the moved Rust gate, crate now at the
  repo root (no working-directory, no paths: filter, coord installed from
  PyPI rather than \`pip install -e .\`).
- .github/workflows/codegen-drift.yml — the phase-3 ADR's generated-types
  gate, byte-comparing src/app/types/generated.rs and
  tests/fixtures/board_sample.json against code-coordinator's generators.
- .github/coord-ci-acceptance.yml — the CI-local coordinator.yml stand-in,
  now a FLAT driver entry keyed on this repo.
- CLAUDE.md — the quadraui pin, the TuiDriver harness rules and the sealed
  suite, moved out of code-coordinator's CLAUDE.md per its scope rule.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
else
    say "WARNING: no scaffold at $SCAFFOLD — the new repo will have no CI"
    fail=1
fi

echo
if [[ "$fail" -ne 0 ]]; then
    say "VERIFICATION FAILED — do not push this. See the FAIL lines above."
    say "The result is at: $OUT_DIR"
    exit 1
fi

say "Extraction verified. Result: $OUT_DIR"
say "History check: git log --follow src/app/data.rs -> $follow_count commits"
echo
cat <<EOF
Next steps (a human/coordinator runs these — this script deliberately will not):

  gh repo create JDonaghy/coord-tui --public \\
      --description "Terminal board for code-coordinator (quadraui-powered)"
  git -C $OUT_DIR remote add origin git@github.com:JDonaghy/coord-tui.git
  git -C $OUT_DIR push -u origin main

Then, in this order (order matters — see #2899):
  1. Pull coord-settings FIRST, then edit coordinator.remote.yml. dellserver's
     live coordinator.yml is a COPY, not a symlink, and thin clients cache
     coordinator.remote.yml — edit before pulling and the next propagation
     silently reverts the new repo entry.
  2. Add the repos:/repo_paths:/acceptance.drivers/capability_rules entries.
     coordinator.example.yml in code-coordinator carries the exact shape.
  3. git clone git@github.com:JDonaghy/coord-tui.git ~/src/coord-tui on every
     machine listed in repo_paths.
  4. coord repo doctor coord-tui && coord status --freshness | grep coord-tui
EOF
