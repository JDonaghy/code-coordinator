# tui-quadraui-workflow skill

**Trigger:** Working on `tui/` (coord-tui) and the task needs the `quadraui`
pin bumped, or needs to build against an unmerged quadraui branch/PR.

**Purpose:** `coord-tui` pins `quadraui` to a git rev in `tui/Cargo.toml`
rather than a relative path, specifically so a `quadraui` merge can never
break `coord-tui`'s build with zero coord-tui commits and no warning (it
happened once, #1973). This skill is the two procedures that pin exists to
support — bumping it deliberately, and working against a not-yet-merged
quadraui feature without touching the pin at all.

---

## Background

`tui/Cargo.toml` has `quadraui = { git = "https://github.com/JDonaghy/quadraui",
rev = "<sha>" }`. `cargo build`/`cargo test` from `tui/` fetch quadraui
straight from GitHub at that pinned rev and never touch `~/src/quadraui` — the
local checkout's branch is irrelevant to a normal build. (#2804:
`scripts/coord-test-runner.sh`'s Test-stage runner used to symlink a
`quadraui` sibling next to every worktree it tested, ONE location shared by
every concurrent run on the machine — that symlink is gone; it was leftover
from before this git-rev pin and was never needed by it.)

## Bumping the pin (deliberate, reviewable, its own coord-tui commit)

1. Pick the target quadraui rev — normally the tip of `origin/develop`
   (quadraui's default/integration branch, per quadraui's own CLAUDE.md).
2. Edit `rev = "..."` in `tui/Cargo.toml`. `cargo update -p quadraui` alone
   will **not** move a `rev`-pinned git dependency — the `Cargo.toml` edit is
   the actual bump.
3. Run `cargo build && cargo test` from `tui/` and confirm `EXIT=0` before
   committing.

## Co-developing against an unmerged quadraui branch/PR

If a `tui/` task consumes a not-yet-merged quadraui feature, the briefing
**must** name the quadraui PR/branch — don't guess at scope from the code
alone.

1. Check out the target branch in `~/src/quadraui`.
2. `cp tui/cargo-config-local-quadraui.toml.example tui/.cargo/config.toml`
   (git-ignored — see the example file's header) to activate cargo's
   local-paths override. **Do not edit `tui/Cargo.toml`** for this — the
   override file is the whole mechanism.
3. Build/test as normal from `tui/`; this now resolves quadraui from the
   local checkout instead of the pinned rev.
4. Before finishing:
   - Verify build `EXIT=0` from `tui/` **with the override active** (proves
     the feature actually works against the target branch).
   - Delete `tui/.cargo/config.toml` (or confirm you never created it) and
     verify build `EXIT=0` again **against the pinned rev** — this is the
     default and what CI/other workers use.
   - Confirm the committed `tui/Cargo.toml` still points at the pinned rev.
     The override file itself is **never** committed.

## Rules

- Never edit `tui/Cargo.toml`'s `rev` to point at a feature branch as a
  shortcut for co-development — use the local-paths override instead. A
  merged `tui/` PR with the pin pointed at someone's feature branch breaks
  every other worker's build the moment that branch is deleted or rebased.
- A pin bump is its own commit, separate from whatever feature work motivated
  it, unless the two are genuinely inseparable.
- `tui/.cargo/config.toml` is git-ignored by design — if `git status` shows it
  as trackable, something is wrong with the ignore rule, not with your
  workflow.
