#!/usr/bin/env bash
#
# coord-test-runner.sh — run the RIGHT tests for a branch, in a throwaway
# worktree, and distinguish a real failure from a flake.
#
#   scripts/coord-test-runner.sh <worktree> [--base-ref REF] [--report FILE]
#                                 [--repo NAME] [--fallback-command CMD]
#                                 [--print-routing]
#
# This is the Test gate's engine.  coord dispatches it as the repo's
# `test_command` (#1426); it is also useful on its own ("did this branch
# actually break anything?").  `coord drive` (#1392, the Python port of the
# former drive-issue.sh) only OBSERVES the verdict this produces.
#
# #1392 kept this as shell on purpose: venv creation is genuinely shell work.
# Its four sharp-edged
# parsers were extracted to tested Python in coord/test_report.py (#1436) —
# that module is the reference behaviour; the grep/awk below still mirrors it.
#
# Six things it handles that a bare `pytest` does not:
#
#  1. PATH ROUTING — code-coordinator ONLY.  Changes under coord/** or
#     tests/** run pytest; a docs-only diff runs nothing and reports SKIP.
#
#     #2899 SHRANK THIS ARM.  It used to be a two-toolchain router because
#     this repo held two codebases: Python under coord/**+tests/**, and the
#     `coord-tui` Rust crate under tui/**.  A single `test_command` could not
#     express that, so the repo earned a hardcoded route.  tui/ now lives in
#     its own `coord-tui` repo, so what is left here is one suite (pytest)
#     behind a path filter — an ordinary repo in all but name.
#
#     The route is DELIBERATELY KEPT rather than collapsed into
#     `--fallback-command pytest`, because `--repo NAME` is supplied by the
#     FLEET's coordinator.yml, which lives in a separate checkout and is
#     updated out-of-band (same reason #2104 made both spellings route here).
#     Deleting the arm before that config change propagates would send every
#     code-coordinator Test dispatch straight to the REFUSE branch below —
#     a red Test gate on every merge, for a config edit nobody had made yet.
#     It also still buys the docs-only SKIP, which the fallback arm's coarser
#     `is_doc_only_path` filter approximates but does not match.
#
#     `--repo NAME` selects this behaviour, and accepts either spelling of
#     this repo's name — `code-coordinator` (since #2104) or the pre-rename
#     `claude-coordinator`.  It defaults to
#     "code-coordinator" for callers that omit it.  EVERY OTHER REPO has no
#     hardcoded routing (#1408) — pass `--fallback-command CMD` (the repo's
#     configured `test_command`, e.g. quadraui's `cargo test --features tui`,
#     or coord-tui's `cargo test`)
#     and the runner treats it as one suite: skip only on a genuinely
#     doc/config-only diff, run it otherwise.  `--repo` given with NO
#     `--fallback-command`, for any repo other than this one, is a
#     repo the runner has no rule for at all — it REFUSES (exit 1) rather than
#     silently reporting SKIP.  A silent green Test gate on unrun tests is
#     exactly the failure mode #1408 exists to close.
#
#  2. THE quadraui PATH DEP — GONE, #2899.  History, kept short because the
#     shape recurs: before #1973 `tui/Cargo.toml` pointed at
#     `../../quadraui/quadraui`, resolved RELATIVE TO THE WORKTREE, so this
#     runner symlinked a `quadraui` sibling at ONE shared location every
#     worktree on the machine reused — the shared-mutable-checkout hazard
#     #2804 is about.  #1973 replaced the path dep with a git-rev pin and
#     #2804 deleted the symlink; #2899 moved the crate out of this repo
#     entirely.  coord-tui now builds itself through `--fallback-command`,
#     like every other repo.  See coord-tui's own CLAUDE.md for the pin.
#
#  3. FLAKE FILTERING — PYTHON ARM ONLY as of #2899.  On failure we
#     re-run ONLY the failed tests, serially and isolated.  If they pass, the
#     run is reported as a flake-tolerated PASS rather than burning an
#     escalated fix round on a test the worker never touched.
#     Build/collection errors are never flake-retried — those are always real.
#
#     KNOWN GAP: the cargo arm had its own flake filter (the tui suite has
#     races under full-parallel `cargo test` — #1260 tracks 3 in
#     commands::tests, plus at least
#     app::tests::plans_panel_capture_key_dispatches_milestone_capture, which
#     is not in that issue).  It went with the arm.  coord-tui now runs
#     through `run_fallback`, which cannot parse an arbitrary command's
#     failure report and so treats every failure as genuine.  Re-adding it
#     means teaching the fallback arm to recognise a `cargo test` command
#     and reuse the parser — deliberately NOT done here, because it would
#     silently change quadraui's and vimcode's verdicts too (a real failure
#     that happens to pass in isolation would start reporting PASS for
#     repos that never asked for that).  It is a follow-up, not an oversight.
#
#  4. `--print-routing` computes the routing decision (which suite(s) would
#     run, or SKIP/REFUSE) and exits WITHOUT building or testing anything —
#     used by the regression tests to assert routing cheaply and deterministically.
#
#  5. TOOLCHAIN RESOLUTION (#1814).  The daemon that runs this (coord-serve, a
#     systemd *user* unit) has a PATH that no login shell ever touched, so a
#     toolchain installed under $HOME can be simply absent and the command
#     dies with "command not found".  Before #1814 that surfaced as a red
#     suite for a branch whose tests were never run.  A missing toolchain and a
#     failing test must never produce the same verdict, so the runner reports
#     `TOOLCHAIN MISSING` and exits 3 — a distinct
#     infrastructure exit code that `coord merge --revalidate` renders as
#     "could not run", never as "SUITE FAILED".  #2899 removed the cargo
#     resolver along with the Rust arm; see the section header below for why
#     the remaining arms do not need one.
#
#  6. BASELINE COMPARISON (#2170).  "Did the suite pass?" is not the question
#     the Test gate is asked — "did this branch make anything WORSE than
#     $BASE_REF on this machine?" is.  Those answers differ exactly when the
#     machine's own baseline is red, and nothing detected that: six tests failed
#     on `origin/main` on any machine with a populated $HOME (invisible to CI,
#     whose $HOME is empty), so the Test stage on `precision` could not produce
#     a green verdict for this repo on ANY branch.  Every dispatch there was
#     reported as the branch's failure and cost a human adjudication.
#     So, when the python arm's failures survive flake filtering, they are
#     re-run against the MERGE-BASE in a scratch worktree.  If every one of
#     them fails there too, the result is `BASELINE-RED` at exit 4 — a distinct
#     outcome that says the branch made nothing worse, not a branch failure.
#     It downgrades a verdict, so it is deliberately unanimous-or-nothing: any
#     test that passes on the base, any branch-new test file, any mechanical
#     problem, and the run reports `FAIL` exactly as before.  Python arm only —
#     see the long note above `ensure_baseline_worktree` for why the fallback
#     arm is excluded rather than "not done yet".
#
#  7. THE POPULATED-$HOME ARM (#2269).  A branch can be green here and red in
#     CI's `populated-home` job, and until #2269 whether that was caught
#     depended on WHICH MACHINE the Test stage landed on.  That job's
#     environment is SYNTHESIZED by scripts/run_tests_in_populated_home.sh (a
#     thin-client ~/.coord with no coordinator.yml, no `sqlite3` on PATH, a
#     $TMPDIR under an ancestor pytest config); this runner otherwise inherits
#     whatever environment the assigned machine happens to have.  dellserver —
#     the daemon host — is structurally incapable of reproducing knob 1, so
#     #2174 passed the Test gate, spent a full adversarial review, and was
#     caught only when `coord fix` noticed CI disagreed.  So once the ordinary
#     python arm is green, the test files THE DIFF TOUCHED are re-run through
#     that same harness.  Diff-scoped because running the suite twice is not
#     affordable (#2169).  This arm can only ever FAIL, never downgrade: the
#     harness's own `exit 2` ("a knob failed to take effect") is a
#     FAIL-with-warning, never a skip.  Python arm only, and it names itself
#     (`python-populated-home`) in every verdict line it emits, so a red result
#     is attributable rather than a mystery.
#
# Exit codes: 0 pass (or skip — nothing to test), 1 genuine failure or refusal
# (cannot determine what to test), 2 usage, 3 INFRASTRUCTURE — the suite could
# not run at all (a required toolchain is missing); no verdict may be inferred
# from it in either direction, 4 BASELINE-RED — the suite ran and failed, but
# identically on $BASE_REF, so no verdict about the BRANCH may be inferred from
# it (the machine's baseline needs fixing).

set -euo pipefail

BASE_REF="origin/main"
REPORT=""
REPO_NAME="code-coordinator"
FALLBACK_CMD=""
PRINT_ROUTING=0

WT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-ref)         BASE_REF="$2"; shift 2 ;;
        --report)           REPORT="$2"; shift 2 ;;
        --repo)             REPO_NAME="$2"; shift 2 ;;
        --fallback-command) FALLBACK_CMD="$2"; shift 2 ;;
        --print-routing)    PRINT_ROUTING=1; shift ;;
        # Print the header comment block: every line from #2 up to (not
        # including) the first line that is not a comment. Computed rather
        # than a hardcoded range, so editing the header can't silently
        # truncate --help (#1392).
        -h|--help)  awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' \
                        "${BASH_SOURCE[0]}"; exit 0 ;;
        -*)         echo "unknown option: $1" >&2; exit 2 ;;
        *)          WT="$1"; shift ;;
    esac
done

[[ -n "$WT" && -d "$WT" ]] || { echo "usage: $0 <worktree> [--base-ref REF]" >&2; exit 2; }
WT="$(cd "$WT" && pwd)"

log()  { printf '    [test] %s\n' "$*"; }
warn() { printf '    [test] !! %s\n' "$*" >&2; }

say() { [[ -n "$REPORT" ]] && printf '%s\n' "$*" >>"$REPORT"; printf '%s\n' "$*"; }
[[ -n "$REPORT" ]] && : >"$REPORT"

# ── what changed? ────────────────────────────────────────────────────────────

DIFF_FAILED=0
if ! CHANGED="$(git -C "$WT" diff --name-only "${BASE_REF}...HEAD" 2>/dev/null)"; then
    warn "could not diff against $BASE_REF — falling back to running everything"
    CHANGED=""
    DIFF_FAILED=1
fi

n_changed="$(printf '%s\n' "$CHANGED" | grep -c . || true)"
log "changed files vs $BASE_REF: $n_changed"

# Paths that are never test-bearing, in ANY repo. A diff touching only these
# is genuinely "nothing to test" — distinct from "could not determine what to
# test", which is a routing gap, not a property of the diff. See #1408.
is_doc_only_path() {
    case "$1" in
        *.md|*.MD|docs/*|LICENSE*|NOTICE*|CHANGELOG*|.gitignore|.github/*) return 0 ;;
        *) return 1 ;;
    esac
}

changed_list() { printf '%s' "$CHANGED" | tr '\n' ' ' | sed 's/ *$//'; }

# ── routing: decide what to run ─────────────────────────────────────────────
#
# This repo is python only since #2899 moved the Rust crate out to coord-tui,
# but it keeps its own hardcoded path routing (below) so a docs-only diff
# still SKIPs precisely, and so the arm survives even if the fleet's
# coordinator.yml has not yet been taught to pass `--fallback-command`
# for it — see header §1. EVERY OTHER REPO has exactly one
# configured test_command (coordinator.yml's `test_command`, passed in here
# as --fallback-command) — the only question there is whether THIS diff is
# doc/config-only (skip) or not (run the one command). A repo that is
# neither this one nor carrying a --fallback-command is one this script has
# no rule for at all: REFUSE rather than silently SKIP (#1408 — a silent
# SKIP there means the merge gate is satisfied by tests that were never
# run).
#
# #2104: BOTH spellings of this repo's name route here. The rename
# (`claude-coordinator` -> `code-coordinator`) lands in this repo, but the
# `--repo NAME` that reaches this script comes from the FLEET's
# coordinator.yml, which lives in a separate checkout and is updated
# out-of-band. Accepting one name only would mean whichever of the two
# changed second silently fell through to the REFUSE branch — a red Test
# gate on every merge, for a config edit nobody had made yet. Accepting both
# makes the two edits order-independent.
RUN_PY=0; RUN_FALLBACK=0
ROUTE_MODE="unknown"

if [[ "$REPO_NAME" == "code-coordinator" || "$REPO_NAME" == "claude-coordinator" ]]; then
    ROUTE_MODE="coordinator"
    if [[ "$DIFF_FAILED" -eq 1 ]]; then
        RUN_PY=1
    else
        while IFS= read -r f; do
            [[ -z "$f" ]] && continue
            # #2896 moved the tui-tuidriver sealed slices out of the repo-root
            # tests/** and #2899 moved the whole crate out of this repo, so
            # the only acceptance slices still under tests/** are the
            # cli-pytest route's own (ms-37).
            case "$f" in
                coord/*|tests/*|pyproject.toml|conftest.py) RUN_PY=1 ;;
            esac
        done <<<"$CHANGED"
    fi
    log "routing: repo=$REPO_NAME pytest=$RUN_PY"
elif [[ -n "$FALLBACK_CMD" ]]; then
    ROUTE_MODE="fallback"
    if [[ "$DIFF_FAILED" -eq 1 ]]; then
        RUN_FALLBACK=1
    else
        while IFS= read -r f; do
            [[ -z "$f" ]] && continue
            is_doc_only_path "$f" || RUN_FALLBACK=1
        done <<<"$CHANGED"
    fi
    log "routing: repo=$REPO_NAME fallback-command run=$RUN_FALLBACK ($FALLBACK_CMD)"
else
    log "routing: repo=$REPO_NAME — no path rule and no --fallback-command"
fi

# ── populated-$HOME arm scoping (#2269) ─────────────────────────────────────
#
# WHICH FILES. Only the python test files THIS DIFF touched, and only ones that
# still exist in the worktree (a diff that DELETES a test file must not hand
# pytest a path that is not there). `tests/acceptance/**` is excluded for the
# same reason the ordinary arm passes `--ignore=tests/acceptance`: that sealed
# suite is gated separately through the #2164 `--ci` wrapper, and a
# red-by-design slice must not fail every concurrent branch.
#
# WHY DIFF-SCOPED AND NOT THE WHOLE SUITE. The full suite already exceeds
# Claude Code's 600s Bash ceiling (#2169); running it a second time under the
# harness is not on the table. Diff-scoping is what makes this arm affordable —
# it costs roughly one test FILE's runtime, paid only by branches that touch
# python tests at all. The always-on cover for the rest of the suite is
# tests/test_ambient_home_isolation.py (which runs everywhere, on every branch);
# this arm exists for the case that file cannot cover, a NEWLY WRITTEN test of
# this class (#2174 was exactly that).
#
# Computed HERE, beside the routing decision, rather than inside `run_python`,
# because `--print-routing` must be able to name the arm before a worker pushes.
POPULATED_TESTS=""
POPULATED_COUNT=0
POPULATED_ARM=0
POPULATED_SKIP_REASON=""

is_python_test_file() {
    case "$1" in
        tests/acceptance/*) return 1 ;;
        tests/*)            ;;
        *)                  return 1 ;;
    esac
    case "${1##*/}" in
        test_*.py) return 0 ;;
        *)         return 1 ;;
    esac
}

if [[ "$ROUTE_MODE" == "coordinator" && "$RUN_PY" -eq 1 ]]; then
    if [[ "$DIFF_FAILED" -eq 1 ]]; then
        POPULATED_SKIP_REASON="the diff against $BASE_REF could not be computed, so the arm has no scope to run over"
    else
        while IFS= read -r f; do
            [[ -z "$f" ]] && continue
            is_python_test_file "$f" || continue
            # Deleted-by-the-diff files are still listed by `git diff --name-only`.
            [[ -f "$WT/$f" ]] || continue
            POPULATED_TESTS="${POPULATED_TESTS:+$POPULATED_TESTS }$f"
            POPULATED_COUNT=$((POPULATED_COUNT + 1))
        done <<<"$CHANGED"
        [[ "$POPULATED_COUNT" -gt 0 ]] && POPULATED_ARM=1
        [[ "$POPULATED_ARM" -eq 0 ]] && \
            POPULATED_SKIP_REASON="the diff touches no python test files under tests/ (outside the sealed tests/acceptance/)"
    fi
elif [[ "$ROUTE_MODE" == "coordinator" ]]; then
    POPULATED_SKIP_REASON="the python arm is not routed for this diff"
else
    POPULATED_SKIP_REASON="the populated-\$HOME arm is this repo's python arm only"
fi

if [[ "$ROUTE_MODE" == "coordinator" ]]; then
    if [[ "$POPULATED_ARM" -eq 1 ]]; then
        log "routing: populated-\$HOME arm (#2269) will re-run $POPULATED_COUNT diff-scoped test file(s): $POPULATED_TESTS"
    else
        log "routing: populated-\$HOME arm (#2269) SKIPPED — $POPULATED_SKIP_REASON"
    fi
fi

if [[ "$PRINT_ROUTING" -eq 1 ]]; then
    case "$ROUTE_MODE" in
        coordinator) printf 'ROUTING mode=coordinator pytest=%s populated-home=%s\n' \
                        "$RUN_PY" "$POPULATED_ARM" ;;
        fallback)    printf 'ROUTING mode=fallback run=%s\n' "$RUN_FALLBACK" ;;
        unknown)     printf 'ROUTING mode=unknown\n' ;;
    esac
    exit 0
fi

if [[ "$ROUTE_MODE" == "unknown" ]]; then
    say "REFUSE: cannot determine what to test for repo '$REPO_NAME' — no path routing rule for it and no configured test_command (--fallback-command) was passed. This must never be recorded as skipped; add test_command to coordinator.yml for this repo, or extend this script's routing."
    exit 1
fi

if { [[ "$ROUTE_MODE" == "coordinator" ]] && [[ "$RUN_PY" -eq 0 ]]; } \
   || { [[ "$ROUTE_MODE" == "fallback" ]] && [[ "$RUN_FALLBACK" -eq 0 ]]; }; then
    say "SKIP: nothing to test — no test-bearing paths changed (docs/config only): $(changed_list)"
    exit 0
fi

FAILED_SUITES=()
FLAKES=()
# Suites that could not run at all (missing toolchain). Kept separate from
# FAILED_SUITES on purpose: these are NOT verdicts (#1814).
INFRA_SUITES=()
EXIT_INFRA=3
# Suites whose every failure reproduces on the merge-base: the machine's
# baseline is red, so this is not a verdict on the branch either (#2170).
BASELINE_RED_SUITES=()
EXIT_BASELINE_RED=4
# See the note at the dispatch site below.
PY_FAIL_SUITE="python"

# ── baseline comparison (#2170) ──────────────────────────────────────────────
#
# THE QUESTION THIS SCRIPT IS ACTUALLY ASKED. Everything above answers "did the
# suite pass?". The Test gate needs "did this branch make anything WORSE than
# $BASE_REF on this machine?". Those differ in exactly one situation — the
# machine's baseline is already red — and until #2170 nothing anywhere detected
# it.
#
# What that cost: six tests failed on `origin/main` on any machine with a
# populated $HOME (a thin-client `~/.coord/`, no `sqlite3` on PATH, a $TMPDIR
# under an ancestor pytest config — all three invisible to CI, whose $HOME is
# empty). So the Test stage on `precision` could not produce a green verdict for
# this repo on ANY branch. Every dispatch returned `SMOKE: fail`, the branch was
# blamed for breakage it did not cause, and a human had to read the log and
# adjudicate — 11m36s and $0.61 for zero signal, repeatedly, for months
# (claude-coordinator#2158). Nothing declared "this machine's baseline is red";
# the failure was indistinguishable from a real regression.
#
# The machinery was already here. Flake filtering re-runs failures serially, so
# the runner ALREADY knows the failing set by name — the only thing missing was
# somewhere to run it that isn't this branch.
#
# THIS IS A DOWNGRADE OF A VERDICT, SO IT IS DELIBERATELY HARD TO TRIGGER.
# Turning a red branch into "not the branch's fault" is the dangerous direction
# — the same direction `is_infrastructure_failure`'s docstring warns about — so
# every ambiguity resolves to the existing `FAIL`:
#
#   * EVERY failing test must also fail on the merge-base. One that passes there
#     is a genuine branch failure and the whole suite reports FAIL as before.
#   * A test whose FILE does not exist on the merge-base is branch-new and can
#     never be "already failing" — inconclusive, so FAIL.
#   * A collection/import error in the baseline run is inconclusive (the base
#     suite never ran) — FAIL.
#   * Anything that goes wrong mechanically (no merge-base, `git worktree add`
#     fails, the shared venv resolves `coord` from the wrong tree) — FAIL, with
#     a warning saying the comparison was skipped rather than answered.
#
# PYTHON ARM ONLY, ON PURPOSE. The fallback arm (any other repo's own
# `test_command`) deliberately gets no baseline check: this script does not know
# an arbitrary command's failure-report format, so it could only compare whole
# exit codes — and it would have to run that command in a bare worktree where
# the repo's own provisioning (`npm ci`, a cargo target dir, a venv) never ran.
# That fails for infrastructure reasons far more often than it detects a red
# baseline, and every such failure would be a FALSE baseline-red, i.e. exactly
# the laundering this guard is written to avoid. The python arm is safe because
# it parses failures by name and can share the branch's already-built venv.
BASELINE_WT=""
BASELINE_SCRATCH=""
BASELINE_DESC=""
BASELINE_TRIED=0

cleanup_baseline() {
    if [[ -n "$BASELINE_WT" ]]; then
        git -C "$WT" worktree remove --force "$BASELINE_WT" >/dev/null 2>&1 || true
    fi
    if [[ -n "$BASELINE_SCRATCH" ]]; then
        rm -rf "$BASELINE_SCRATCH" || true
    fi
}
trap cleanup_baseline EXIT

# Lazily create a detached worktree at the merge-base, setting BASELINE_WT and
# BASELINE_DESC. Non-zero (with a warning) when it cannot be created — callers
# must then keep whatever verdict they already had. Created lazily because it is
# only ever needed on the genuine-failure path, the already-expensive one.
#
# Communicates through globals and NOT by echoing the path, deliberately: a
# `$(baseline_worktree)` call site would run this in a subshell, where the
# BASELINE_* assignments (and therefore `cleanup_baseline`'s ability to remove
# the worktree) are discarded, and where `log`'s stdout would be captured into
# the caller's variable instead of the transcript.
ensure_baseline_worktree() {
    if [[ -n "$BASELINE_WT" ]]; then
        return 0
    fi
    if [[ "$BASELINE_TRIED" -eq 1 ]]; then
        return 1
    fi
    BASELINE_TRIED=1

    local mb
    if ! mb="$(git -C "$WT" merge-base HEAD "$BASE_REF" 2>/dev/null)" || [[ -z "$mb" ]]; then
        warn "baseline: cannot resolve the merge-base of HEAD and $BASE_REF — reporting the failure as the branch's, uncompared"
        return 1
    fi
    BASELINE_SCRATCH="$(mktemp -d)"
    local path="$BASELINE_SCRATCH/baseline"
    if ! git -C "$WT" worktree add --detach "$path" "$mb" >/dev/null 2>&1; then
        warn "baseline: could not check out $mb in a scratch worktree — reporting the failure as the branch's, uncompared"
        return 1
    fi
    BASELINE_WT="$path"
    BASELINE_DESC="the merge-base (${mb:0:12}) of HEAD and $BASE_REF"
    log "baseline: comparing against $BASELINE_DESC"
    return 0
}

# 0 == every node id in $1 also fails on the merge-base (baseline is red).
# Non-zero == the branch owns the failure, or the question could not be
# answered. Never anything in between: see the "hard to trigger" note above.
python_baseline_is_red() {
    local failed="$1"
    local venv="$WT/.venv"
    local base_wt f file

    ensure_baseline_worktree || return 1
    base_wt="$BASELINE_WT"

    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        file="${f%%::*}"
        if [[ ! -e "$base_wt/$file" ]]; then
            log "baseline: $file does not exist on the merge-base — branch-new, so the baseline cannot be red for it"
            return 1
        fi
    done <<<"$failed"

    # The baseline run reuses the BRANCH's venv (building a second one would
    # double a Test leg's cost, see #2169) and relies on `python -m pytest`
    # putting the CWD first on sys.path so `import coord` resolves from the
    # baseline worktree. That is true for this project's flat-layout editable
    # install, but it is a property of how pip chose to write the install — not
    # something to assume. Assert it, and skip the comparison rather than
    # silently compare the BRANCH's `coord` against the base's tests.
    if ! (cd "$base_wt" && "$venv/bin/python" -c "
import pathlib, sys
import coord
sys.exit(0 if str(pathlib.Path(coord.__file__).resolve()).startswith('$base_wt') else 9)
") >/dev/null 2>&1; then
        warn "baseline: the branch venv does not resolve 'coord' from the baseline worktree — reporting the failure as the branch's, uncompared"
        return 1
    fi

    local out="$WT/.pytest.baseline.out"
    log "baseline: re-running the failing test(s) on the merge-base"
    # shellcheck disable=SC2086  # node ids are intentionally word-split
    if (cd "$base_wt" && "$venv/bin/python" -m pytest -q --tb=short $failed) >"$out" 2>&1; then
        log "baseline: every failing test PASSES on the merge-base — the branch owns this failure"
        return 1
    fi
    if grep -qE "^(ERROR|INTERNALERROR)" "$out"; then
        warn "baseline: the merge-base run hit a collection/import error, so it says nothing about these tests — reporting the failure as the branch's, uncompared"
        return 1
    fi

    local base_failed
    base_failed="$(grep '^FAILED ' "$out" | awk '{print $2}' | sort -u || true)"
    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        if ! printf '%s\n' "$base_failed" | grep -qxF "$f"; then
            log "baseline: $f PASSES on the merge-base — a genuine branch failure"
            return 1
        fi
    done <<<"$failed"
    return 0
}

mark_baseline_red() {
    BASELINE_RED_SUITES+=("$1")
}

# #2170 review (non-blocking): CONTENTS, not lengths. `mark_baseline_red` is
# only ever called with the literal "python" today, so
# `${#BASELINE_RED_SUITES[@]} -eq ${#FAILED_SUITES[@]}` happens to be a valid
# proxy for "every failed suite was confirmed baseline-red" — but only
# because BASELINE_RED_SUITES can currently never be anything but `()` or
# `("python")`. If baseline comparison is later extended to the rust/
# fallback arms, a length-only match could silently misfire on a
# genuinely-mixed failure set (one suite confirmed baseline-red, a
# DIFFERENT suite genuinely broken, coincidentally the same count). Compare
# sorted contents instead so that stays correct regardless of which arms
# ever call `mark_baseline_red`.
baseline_red_covers_all_failures() {
    [[ ${#BASELINE_RED_SUITES[@]} -eq 0 || ${#FAILED_SUITES[@]} -eq 0 ]] && return 1
    local sorted_failed sorted_baseline_red
    sorted_failed="$(printf '%s\n' "${FAILED_SUITES[@]}" | sort)"
    sorted_baseline_red="$(printf '%s\n' "${BASELINE_RED_SUITES[@]}" | sort)"
    [[ "$sorted_failed" == "$sorted_baseline_red" ]]
}

# ── toolchain resolution (#1814) ─────────────────────────────────────────────
#
# `coord serve` is a systemd USER unit. Its PATH is systemd's
# (`systemctl --user show-environment`), not a login shell's — ~/.profile and
# the shell rcs that extend PATH are never sourced for it. So a toolchain that
# resolves fine over ssh can be entirely absent inside the daemon, and
# `coord merge --revalidate` reported that as a red suite for a branch CI had
# already proven green.
#
# #2899 narrowed what this section has to resolve. The original case was
# `~/.cargo/bin/cargo` for this repo's in-tree Rust arm; that arm left with
# `tui/` (see header §1/§2). What remains is `python3` for the python arm and
# whatever `--fallback-command` names for every other repo — and the fallback
# arm runs through `bash -lc`, a LOGIN shell, which does source the rcs. So
# cargo-using repos (coord-tui, quadraui, vimcode) get their PATH the ordinary
# way and need no resolver here; they are still covered against a genuinely
# missing toolchain by `run_fallback`'s exit-127 check below.
#
# `toolchain_missing` is the one and only way this script reports "could not
# run": one message, naming the tool, the places searched, and the PATH it
# searched them from.
toolchain_missing() {
    local tool="$1" suite="$2" searched="$3"
    say "TOOLCHAIN MISSING($suite): '$tool' not found — searched: $searched"
    say "      This is an INFRASTRUCTURE failure, not a test failure: the $suite suite never ran, so nothing about the branch may be inferred from it (#1814)."
    say "      If this ran from the coord-serve daemon, note that a systemd user unit's PATH is not a login shell's — ~/.profile is never sourced for it. Install the toolchain where the daemon can see it, or set Environment=PATH= in deploy/coord-serve.service."
    say "      PATH=$PATH"
    INFRA_SUITES+=("$suite")
}

# ── python ───────────────────────────────────────────────────────────────────

# #2180 (review fix): run the sealed cli-pytest acceptance route (ms-37)
# through the SAME #2164 `--ci` wrapper CI's `test` job uses
# (.github/workflows/test.yml's "coord acceptance run --all --ci
# (cli-pytest route, ms-37)" step), instead of just excluding it. Excluding
# it outright (the original #2180 change) left a gap the reviewer caught:
# an `expected_red`-listed test that unexpectedly PASSES (the #1965
# vacuous-assertion case #2164 exists to catch) would be silently waved
# through by the Test stage — it never ran that suite at all — while CI's
# `test` job (no continue-on-error on this step) correctly reddens for the
# same branch. That is exactly the "Test stage tolerates what CI rejects"
# split the issue's acceptance criteria forbid. The wrapper is what closes
# it: `--ci` still tolerates a red-by-design slice (so a concurrent branch
# isn't punished for someone else's in-flight fix, the quadraui#554/#490
# failure mode #2180 exists to close) while turning a test that passes when
# it shouldn't into a hard failure.
#
# `--config` points at the SAME in-repo .github/coord-ci-acceptance.yml
# fragment CI's own steps use — NOT the daemon host's real
# ~/.coord/coordinator.yml, even though this script runs on that host and
# every other `coord` invocation there resolves it by default. The first
# version of this function did exactly that ("the fleet config's routes
# mirror the fragment verbatim") and the assumption was wrong in the one
# way the fragment's own header warns about: the fleet's cli-pytest route
# is written for `--issue` scoping (`pytest tests/acceptance/{ms}`), and
# `--all` always passes `ms=None`, which render_run_command documents as
# leaving `{ms}` UNSUBSTITUTED — so pytest tried to collect a path
# literally named `tests/acceptance/{ms}`, collected 0 tests, and this arm
# reported the sealed suite red for every branch touching coord/**
# (first bitten: issue-2235's Test leg, 2026-08-15). The fragment's routes
# are written to cover the whole accumulated suite with no `{ms}`
# reference at all, which is exactly the `--all` contract; it ships in the
# same tree as this script, so the copy in $WT always matches the branch
# under test.
#
# The PATH prefix matters for the same reason: the fragment's route is a
# bare `pytest tests/acceptance`, resolved from PATH by the driver's
# shell. CI activates its job venv so `pytest` lands on the right
# interpreter; here `coord` is invoked by absolute path from a venv whose
# bin/ was never put ON the PATH, and the daemon's own PATH (a systemd
# user unit — see the #1814 toolchain notes above) has no pytest with this
# repo's deps, or none at all.
#
# The scratch $HOME + cleared COORD_SERVICE_URL/COORD_TOKEN/COORD_CONFIG
# are what make the `--config` above actually STICK. `_load_config`
# (coord/commands/_common.py, #1080) treats "is a board service
# configured" as the PRIMARY branch, checked BEFORE the explicit --config
# path: on any machine with ~/.coord/client.toml or $COORD_SERVICE_URL
# set (a thin client — or a daemon host whose client.toml points at its
# own board service), it fetches the fleet's remote coordinator.yml and
# silently overwrites the path we just passed. That remote config's
# cli-pytest route is the {ms}-templated one this whole function exists
# to avoid — the first --config-only version of this fix went red on
# exactly that (`tests/acceptance/{ms}`: 0 collected, 2026-08-15,
# issue-2235's second Test leg). #2208 is fixing the override-discard in
# _load_config itself; until then — and even after, as hermeticity —
# this invocation must look like a CI runner: no client.toml in $HOME
# (a scratch dir inside $WT, same lifecycle as .pytest-acceptance.out),
# no service env vars, so `resolve_board_service` returns None and the
# explicit --config is honoured. Nothing in this codepath needs the real
# $HOME: config comes from --config, the checkout from --path, and the
# board-snapshot side-write is best-effort into $HOME/.coord (now the
# scratch dir, exactly like a GitHub Actions runner's blank home).
#
# Called only after the ordinary suite above has already passed (or been
# judged a tolerated flake) — same order CI enforces implicitly by running
# this as a later, unguarded step: a genuinely broken ordinary suite must
# not be masked by (or additionally blamed on) the acceptance route.
run_python_acceptance_ci() {
    local venv="$1"
    local acc_out="$WT/.pytest-acceptance.out"
    # Scratch home so `coord`'s thin-client branch can't see a real
    # ~/.coord/client.toml and hijack --config (see header). Left in place
    # like .pytest-acceptance.out — $WT is a disposable worktree.
    local ci_home="$WT/.coord-ci-home"
    mkdir -p "$ci_home"
    log "running: coord acceptance run --all --ci (cli-pytest route, ms-37)"
    local rc=0
    (env -u COORD_SERVICE_URL -u COORD_TOKEN -u COORD_CONFIG \
            HOME="$ci_home" PATH="$venv/bin:$PATH" \
            "$venv/bin/coord" acceptance run \
            --repo claude-coordinator --all --ci \
            --config "$WT/.github/coord-ci-acceptance.yml" \
            --for-path coord/cli.py --path "$WT") >"$acc_out" 2>&1 || rc=$?
    if [[ "$rc" -eq 0 ]]; then
        say "PASS(python): ordinary suite + sealed acceptance suite (ms-37, cli-pytest) both green"
        return 0
    fi
    # #2936: 127 is the shell's "command not found" for `$venv/bin/coord`
    # itself — the SAME infrastructure class as the missing-python3/missing-
    # fallback-command cases above (#1814), NOT a red suite. This venv is the
    # one `run_python` just built (or found already usable) and successfully
    # ran the ordinary pytest suite from moments ago, so a missing
    # `$venv/bin/coord` here means this package's own console-script entry
    # point never landed in it — an environment problem, not something the
    # branch under test can be blamed for. Before this check, "coord:
    # command not found" (#2936 — a smoke worker unable to resolve `coord` at
    # all) and a genuinely red sealed suite were indistinguishable here, and
    # the missing binary silently laundered into `RESULT: FAIL`.
    if [[ "$rc" -eq 127 ]]; then
        toolchain_missing coord python-acceptance-cli "\$venv/bin ($venv/bin), then \$PATH"
        tail -n 20 "$acc_out" | sed 's/^/      /'
        return 1
    fi
    say "FAIL(python): sealed acceptance suite (cli-pytest route, ms-37) failed under --ci — either a test-id NOT listed in expected_red failed for real, or one listed in expected_red unexpectedly passed (see .github/workflows/test.yml's 'A red result here is attributable, not a mystery' step for the two-cause triage)"
    tail -n 40 "$acc_out" | sed 's/^/      /'
    return 1
}

# ── the populated-$HOME arm (#2269) ─────────────────────────────────────────
#
# Re-run the diff's own python test files in the environment of a REAL FLEET
# MACHINE instead of this machine's, using the SAME harness CI's
# `populated-home` job uses (scripts/run_tests_in_populated_home.sh, from the
# BRANCH's tree — so a branch that changes the harness is tested against its own
# version, exactly like CI). See item 7 in the header for why this is not
# optional: without it, catching this failure class is a property of which
# machine the Test stage was routed to.
#
# THIS ARM ONLY EVER FAILS. It is the mirror image of #2170's baseline check,
# which only ever downgrades and is therefore deliberately hard to trigger. This
# one is the STRICT direction, so every ambiguity resolves the other way:
#
#   * The harness's own `exit 2` — "a knob failed to take effect", i.e. the
#     environment it claims to synthesize was not synthesized — is a FAIL with a
#     warning, NOT a skip. A guard that cannot guard reporting green is the exact
#     failure mode #2170 exists to prevent, and a Test gate that silently
#     tolerates it is back to routing luck.
#   * Any other non-zero exit is a FAIL naming this arm.
#
# There is no flake filter here on purpose: these same files went green in the
# ordinary arm moments ago, so a failure here is by construction environmental,
# and re-running would only launder it.
#
# The two SKIPs it can report (no python test files in the diff; the harness
# absent from this branch's tree) are announced in the verdict stream and by
# `--print-routing`, never silent.
run_python_populated_home() {
    local venv="$1"
    local rel="scripts/run_tests_in_populated_home.sh"
    local script="$WT/$rel"

    if [[ "$POPULATED_ARM" -eq 0 ]]; then
        say "SKIP(python-populated-home): not run — $POPULATED_SKIP_REASON (#2269)"
        return 0
    fi
    if [[ ! -f "$script" ]]; then
        # A branch (or a checkout) predating the harness. Skipping is the only
        # option that does not redden every such branch, but it is stated out
        # loud rather than inferred from silence.
        warn "populated-home: $rel is not present in this worktree — the arm cannot run"
        say "SKIP(python-populated-home): not run — the harness $rel is absent from this branch's tree (#2269)"
        return 0
    fi

    local out="$WT/.pytest-populated-home.out"
    local started=$SECONDS
    local rc=0
    log "running: $rel over $POPULATED_COUNT diff-scoped test file(s): $POPULATED_TESTS"
    # shellcheck disable=SC2086  # the file list is intentionally word-split
    (cd "$WT" && bash "$script" "$venv/bin/python" -m pytest -q --tb=short $POPULATED_TESTS) \
        >"$out" 2>&1 || rc=$?
    local elapsed=$((SECONDS - started))

    if [[ "$rc" -eq 0 ]]; then
        say "PASS(python-populated-home): $POPULATED_COUNT diff-scoped test file(s) green in a synthesized fleet \$HOME (+${elapsed}s)"
        return 0
    fi

    # The harness's own guard failures. `run_tests_in_populated_home.sh`
    # reserves exit 2 for exactly this case, so check `rc` directly first —
    # it can't drift out of sync with itself. The message grep is a second,
    # belt-and-braces signal for a harness that fails this way but exits with
    # some other code (or an older/forked copy of the script); if the exact
    # wording ever changes, `rc -eq 2` alone still catches the exit-2 case.
    # Distinguished from a test failure because the two need different fixes
    # — but BOTH are a FAIL, so mis-classifying one as the other can only
    # ever mis-word a red verdict, never hide one.
    if [[ "$rc" -eq 2 ]] || grep -qE "the guard itself is broken|the guard is too broad|is not thin-client shaped" "$out"; then
        say "FAIL(python-populated-home): the harness $rel exited $rc because A KNOB FAILED TO TAKE EFFECT — the environment it claims to synthesize (thin-client ~/.coord, no sqlite3, \$TMPDIR under an ancestor pytest config) was not the environment the tests ran in, so a green here would have meant nothing. This is reported as a FAILURE, not a skip, on purpose (#2269)."
        tail -n 20 "$out" | sed 's/^/      /'
        return 1
    fi

    say "FAIL(python-populated-home): $POPULATED_COUNT diff-scoped test file(s) PASS in this machine's ambient environment but FAIL in a synthesized fleet \$HOME (thin-client ~/.coord with no coordinator.yml, no sqlite3 on PATH, \$TMPDIR under an ancestor pytest config) — this is CI's populated-home job's failure class, caught here instead of at the merge gate (#2269). Reproduce with: $rel python -m pytest $POPULATED_TESTS"
    printf '%s\n' "$POPULATED_TESTS" | tr ' ' '\n' | sed 's/^/      /'
    tail -n 40 "$out" | sed 's/^/      /'
    return 1
}

# The arms that run AFTER the ordinary pytest suite has gone green (or been
# judged a tolerated flake), in cost order. Short-circuiting on the first
# failure is deliberate: the verdict is already attributable to a named arm, and
# there is nothing to learn from paying for the next one.
run_python_post_arms() {
    local venv="$1"
    run_python_acceptance_ci "$venv" || return 1
    run_python_populated_home "$venv" || { PY_FAIL_SUITE="python-populated-home"; return 1; }
    return 0
}

run_python() {
    local venv="$WT/.venv"
    if [[ ! -x "$venv/bin/python" ]]; then
        # Same class as the cargo case below (#1814): with no interpreter there
        # is no suite, and "no suite" is not a verdict.
        if ! command -v python3 >/dev/null 2>&1; then
            toolchain_missing python3 python "PATH"
            return 1
        fi
        log "creating venv + installing .[dev] (~12s)"
        python3 -m venv "$venv" >/dev/null
        "$venv/bin/pip" install -q -e "$WT[dev]" >/dev/null 2>&1 || {
            say "FAIL(python): could not install .[dev] — environment problem, not a code failure"
            return 1
        }
    fi

    # Use xdist when the BRANCH's pyproject.toml pulls it in (~2.4x faster:
    # 5m49s → 1m36s on a 4642-test suite, identical results). Detected rather
    # than assumed, because the venv is built from the branch under test and
    # any branch predating the dev-dep would die on an unknown -n flag.
    local par=()
    if "$venv/bin/python" -c "import xdist" 2>/dev/null; then
        par=(-n auto)
        log "pytest-xdist present → running in parallel"
    fi

    local out="$WT/.pytest.out"
    log "running: pytest -q ${par[*]:-(serial)} (full suite)"
    # #2180: --ignore=tests/acceptance — the sealed oracle-loop suite
    # (tests/acceptance/ms-NN/) is split out of the ORDINARY suite here and
    # gated separately, below, through `coord acceptance run --all --ci`
    # (#2164, run_python_acceptance_ci — see its header comment), which
    # honours each ms-NN/manifest.yml's `expected_red:` registry. Left in
    # this plain `pytest` invocation, a red-by-design slice would fail the
    # Test stage for every concurrent branch in the repo, not just the one
    # the slice belongs to (the quadraui#554/#490 failure mode #2180 exists
    # to close). Matches CI's own .github/workflows/test.yml split — same
    # reasoning applies verbatim, see that file's comments. ms-33/ms-38
    # (Rust) and ms-51 (Playwright) were already outside this pytest arm's
    # reach; ms-37 (plain .py files under testpaths=["tests"]) was the one
    # accidental gap — and the one this arm still covers, just through the
    # wrapper instead of directly.
    #
    # ${par[@]+...} so an empty array is not an unbound-variable error under
    # `set -u` on older bash.
    if (cd "$WT" && "$venv/bin/python" -m pytest -q --tb=short --ignore=tests/acceptance ${par[@]+"${par[@]}"}) >"$out" 2>&1; then
        log "ordinary suite: $(grep -oE '[0-9]+ passed[^)]*' "$out" | tail -1)"
        run_python_post_arms "$venv"
        return $?
    fi

    # A collection/import error is never a flake — the suite could not even run.
    if grep -qE "^(ERROR|INTERNALERROR)" "$out"; then
        say "FAIL(python): collection/import error"
        tail -n 30 "$out" | sed 's/^/      /'
        return 1
    fi

    local failed
    failed="$(grep '^FAILED ' "$out" | awk '{print $2}' | sort -u || true)"
    if [[ -z "$failed" ]]; then
        say "FAIL(python): non-zero exit with no parseable FAILED lines"
        tail -n 30 "$out" | sed 's/^/      /'
        return 1
    fi

    local count; count="$(printf '%s\n' "$failed" | grep -c . || true)"
    log "$count test(s) failed — re-running them in isolation to filter flakes"
    local rerun="$WT/.pytest.rerun.out"
    # shellcheck disable=SC2086  # node ids are intentionally word-split
    if (cd "$WT" && "$venv/bin/python" -m pytest -q --tb=short $failed) >"$rerun" 2>&1; then
        say "FLAKE(python): $count test(s) failed in the full run but PASS in isolation"
        printf '%s\n' "$failed" | sed 's/^/      /'
        FLAKES+=("python:$count")
        run_python_post_arms "$venv"
        return $?
    fi

    # #2562: the isolation re-run above is not all-or-nothing evidence — its
    # own `FAILED ` lines say EXACTLY which node ids survived. Rebuild
    # $failed from those before doing anything else: a test that dropped out
    # PASSED in isolation and is by definition the flake case this whole
    # block exists to catch. Handing the ORIGINAL (unpruned) set to the
    # baseline comparison below is the bug this closes — one genuine failure
    # was keeping a flake in the set, and the baseline comparison is itself
    # all-or-nothing (#2170's unanimity), so the flake alone could suppress a
    # correct BASELINE-RED downgrade.
    local still_failed
    still_failed="$(grep '^FAILED ' "$rerun" | awk '{print $2}' | sort -u || true)"
    if [[ -z "$still_failed" ]]; then
        # Non-zero exit but no parseable FAILED lines — e.g. a collection/
        # import error on the isolated re-run. Nothing to prune against;
        # fall back to the original set rather than claim a pruning that
        # did not happen.
        still_failed="$failed"
    fi
    local pruned_count; pruned_count="$(printf '%s\n' "$still_failed" | grep -c . || true)"
    if [[ "$pruned_count" -lt "$count" ]]; then
        log "$((count - pruned_count)) of $count failing test(s) passed in isolation — treating as flakes, comparing the remaining $pruned_count against the baseline"
        FLAKES+=("python:$((count - pruned_count))")
    fi
    failed="$still_failed"
    count="$pruned_count"

    # Not a flake. Before calling it the branch's fault, ask the question the
    # Test gate is actually asked: is this worse than $BASE_REF on THIS machine?
    # (#2170 — see the baseline section above for why this only ever downgrades
    # on unanimous, positively-confirmed evidence.)
    if python_baseline_is_red "$failed"; then
        say "BASELINE-RED(python): all $count failing test(s) fail identically on $BASELINE_DESC — this machine's baseline is red and this is NOT a verdict on the branch"
        printf '%s\n' "$failed" | sed 's/^/      /'
        say "      The branch made nothing worse. Fix the BASELINE (or this machine's environment) — a machine whose suite cannot go green cannot produce a Test verdict for any branch (#2170). Reproduce the environment half with scripts/run_tests_in_populated_home.sh."
        mark_baseline_red python
        return 1
    fi

    # Deliberately NOT worded "and not reproducible on $BASE_REF": the baseline
    # comparison may have been skipped (no merge-base, worktree add failed, …),
    # and claiming a comparison that did not happen is how a runner earns
    # distrust. The `[test] baseline: …` lines above say which of the two it was.
    say "FAIL(python): $count test(s) fail on re-run — genuine"
    printf '%s\n' "$failed" | sed 's/^/      /'
    tail -n 40 "$rerun" | sed 's/^/      /'
    return 1
}

# ── fallback (any repo's own configured test_command) ───────────────────────
#
# One suite, no flake filtering — this script does not know the failure-report
# format of an arbitrary command, so a fallback failure is always reported as
# genuine rather than flake-retried. The repo builds and tests itself; unlike
# code-coordinator's tui/ there is no known cross-repo path dep to wire up
# here (if one shows up for a given repo, that repo earns its own case here).
run_fallback() {
    local out="$WT/.fallback.out"
    local rc=0
    log "running: $FALLBACK_CMD"
    (cd "$WT" && bash -lc "$FALLBACK_CMD") >"$out" 2>&1 || rc=$?
    if [[ "$rc" -eq 0 ]]; then
        say "PASS(fallback): $FALLBACK_CMD"
        return 0
    fi
    # 127 is the shell's "command not found" — the suite never started, so this
    # is the same infrastructure class as a missing cargo above (#1814) and must
    # not be reported as a failing suite.
    if [[ "$rc" -eq 127 ]]; then
        toolchain_missing "${FALLBACK_CMD%% *}" fallback "the login shell's PATH"
        tail -n 20 "$out" | sed 's/^/      /'
        return 1
    fi
    say "FAIL(fallback): $FALLBACK_CMD exited $rc"
    tail -n 40 "$out" | sed 's/^/      /'
    return 1
}

# ── drive ────────────────────────────────────────────────────────────────────

# A suite whose failure was already recorded in INFRA_SUITES (via
# toolchain_missing) must NOT also land in FAILED_SUITES: the INFRA_SUITES
# check below always runs first and exits before FAILED_SUITES is consulted,
# so double-booking was harmless, but it left a suite name in a "these are
# verdicts" list it never actually earned — confusing for a future reader of
# either list on its own (#1814 review).
mark_failed() {
    local suite="$1" s
    for s in ${INFRA_SUITES[@]+"${INFRA_SUITES[@]}"}; do
        [[ "$s" == "$suite" ]] && return 0
    done
    FAILED_SUITES+=("$suite")
}

# Which name a failing python arm books itself under. "python" for every arm
# that existed before #2269; the populated-$HOME arm overwrites it with its own
# name so `RESULT: FAIL (python-populated-home)` says which environment went
# red, not merely that pytest did (#2269's "a red result is attributable"
# requirement). It is only ever read after `run_python` returns non-zero.
if [[ "$RUN_PY" -eq 1 ]]; then
    run_python || mark_failed "$PY_FAIL_SUITE"
fi
if [[ "$RUN_FALLBACK" -eq 1 ]]; then
    run_fallback || mark_failed fallback
fi

if [[ ${#FLAKES[@]} -gt 0 ]]; then
    say "NOTE: flakes tolerated this run: ${FLAKES[*]} — see #1260"
fi

# Checked BEFORE the FAIL branch and with its own exit code: a suite that could
# not run is not a suite that failed, and collapsing the two is the #1814 bug.
# A suite that genuinely failed alongside one that could not run still reports
# INFRA — the run as a whole is untrustworthy until the environment is fixed.
if [[ ${#INFRA_SUITES[@]} -gt 0 ]]; then
    say "RESULT: INFRA (${INFRA_SUITES[*]}) — the suite could not run; this is NOT a test failure and no verdict may be recorded from it"
    exit "$EXIT_INFRA"
fi

# Same precedence logic as INFRA, one rung lower: a suite that was already red
# on the merge-base is not a suite this branch failed. Checked BEFORE the FAIL
# branch, and only when NO suite failed for a branch-attributable reason — a
# genuinely-broken rust arm alongside a baseline-red python arm is still a
# branch FAIL, because the branch did break something (#2170).
if baseline_red_covers_all_failures; then
    say "RESULT: BASELINE-RED (${BASELINE_RED_SUITES[*]}) — every failure reproduces on $BASE_REF in this environment; the branch made nothing worse, so this is NOT a branch failure and must not consume a fix attempt"
    exit "$EXIT_BASELINE_RED"
fi

if [[ ${#FAILED_SUITES[@]} -gt 0 ]]; then
    say "RESULT: FAIL (${FAILED_SUITES[*]})"
    exit 1
fi
say "RESULT: PASS"
exit 0
