# coord-tui

The terminal board for [code-coordinator](https://github.com/JDonaghy/code-coordinator):
a live pipeline view, a machines panel, an embedded `claude` PTY pane, and one-key
stage-to-stage handoffs. Rust, ratatui, [quadraui](https://github.com/JDonaghy/quadraui).

> **This repo was extracted from `code-coordinator`'s `tui/` subdirectory, with history
> (#2899, Phase 4 of code-coordinator#2894).** `git log --follow src/app/data.rs` reaches
> back past the move. If you are looking for why something is the way it is and the trail
> seems to stop, it does not — keep following.

## Working on this repo — the `quadraui` pin

**`coord-tui` pins `quadraui` to a git rev in `Cargo.toml`**
(`quadraui = { git = "https://github.com/JDonaghy/quadraui", rev = "<sha>" }`, #1973) —
never edit that `rev` as a shortcut for co-development, and never build against whatever
happens to be checked out in `~/src/quadraui`; a quadraui merge broke this crate's build
with zero commits here and no warning once already, which is exactly what the pin prevents.
Bumping the pin deliberately, and building against an unmerged quadraui branch/PR without
touching the pin, are both procedures — see the `tui-quadraui-workflow` skill
(shipped by `coord install-skills`) for the steps.

The one local-path override (`cargo-config-local-quadraui.toml.example`, copied to the
git-ignored `.cargo/config.toml`) is an opt-in, human-attended co-dev workflow that
**must be reverted before a branch is pushed**.

## Testing — black-box coverage is the acceptance bar

**Every PR that changes user-visible behavior must ship a black-box test** that drives the
running app and asserts on its rendered output. The adversarial reviewer reads this file and
**rejects behavior-changing PRs that lack one** (pure refactors / internal-only changes are
exempt — say so in the PR if that applies).

### quadraui `TuiDriver` (harness shipped: #690 / #691)
- Drives the whole app through the real `event → handle → render` path against ratatui's
  headless `TestBackend` and asserts on the screen grid. `cargo test`-native, deterministic,
  no TTY.
- `CoordApp` implements quadraui's `ShellApp`, so use
  `quadraui::tui::testing::driver_with_shell(app, CoordApp::shell_config(), w, h)`.
  API: `find("text")` → coords, `click(x, y)`, `press`, `type_char`, `screen()`,
  `screen_contains(needle)`. **Locate targets with `find` — never hardcode coordinates.**
- **Reuse the existing fixtures** — `make_test_app(data: BoardData) -> CoordApp` (and
  `make_app_with_assignments`, `make_app_with_one_completed_issue`, …) in `src/app.rs` build
  a full app from in-memory `BoardData`, no live daemon. Put the tests **in-crate**
  (`#[cfg(test)]`), **not** in `tests/` — the fixtures are `#[cfg(test)]`/private and an
  integration-test crate can't see them.
- Limit: `TuiDriver` renders to `TestBackend`, so it does **not** parse real ANSI —
  terminal-protocol bugs (raw-mode, SGR mouse, the embedded `claude` PTY pane) are out of
  reach and still need a live smoke. A native pty + vt100 tier is tracked in quadraui#302
  (unbuilt). Reserve SMOKE_TESTS bullets for exactly that blind spot; anything reachable
  through `TuiDriver` belongs in an in-crate test, because the automated headless smoke is
  the gate.

## Rules for workers

- **Never edit the sealed suite — `tests/acceptance.rs` *and* `tests/acceptance/**`.**
  `tests/acceptance.rs` is the `tui-tuidriver` driver's `entrypoint:`, and an entrypoint is
  sealed as a whole file; the sibling `tests/acceptance/` directory it `include!`s is derived
  from that path and sealed too. Do not be reassured by the entrypoint's prologue calling
  itself "a seam smoke test": *any* `type="work"` diff touching a sealed path is an
  **unconditional, mandatory `request-changes`**. Write your own in-crate `#[cfg(test)]`
  tests instead. The authoritative list is `AcceptanceConfig.sealed_paths()` in
  code-coordinator's `coord/config.py`, derived from `.github/coord-ci-acceptance.yml`'s
  configured `entrypoint:`.
- **Two committed files are GENERATED — never hand-edit them:**
  `src/app/types/generated.rs` (code-coordinator's `scripts/codegen.py --rust`) and
  `tests/fixtures/board_sample.json` (its `scripts/gen_board_fixture.py`). Both are
  byte-compared by `.github/workflows/codegen-drift.yml`. A red there usually means
  code-coordinator's wire schema moved and this repo has not caught up — re-run the
  generator and commit its output; do not patch the file by hand to make the diff go away.
  A stale `generated.rs` deserialises `/board` into the wrong shape and blanks the entire
  board, silently (#632/#546/#628).
- **Stay in file scope.** If you must touch a file outside your briefing, note it in your
  final message.
- **Commit and push before your final message** — even if the build is broken or you ran out
  of time. Uncommitted work is destroyed when the session ends.
- **`gh` is on the deny-list.** The coordinator owns all GitHub interaction; use plain `git`.
- **Only the coordinator writes docs.** Do not update README or shared documentation files —
  parallel doc edits cause merge conflicts.

## Build & test

```bash
cargo build                 # debug binary at target/debug/coord-tui
cargo build --features gtk --bin coord-tui-gtk   # needs GTK4 dev libs
cargo test                  # the ordinary suite
cargo test <name_filter>    # scope it — workers, prefer this
```

`cargo test` deliberately does NOT run the sealed acceptance target: it declares
`required-features = ["test-support"]` in `Cargo.toml`, so cargo silently skips it without
the flag (#1042 pinned that on purpose, so a driver invocation missing the flag fails loudly
instead of reporting a vacuous "0 passed").

**Known flakes.** This suite has races under full-parallel `cargo test` — #1260 tracks three
in `commands::tests`, plus at least
`app::tests::plans_panel_capture_key_dispatches_milestone_capture`, which is not in that
issue. Before #2899 code-coordinator's `scripts/coord-test-runner.sh` re-ran failures
serially and reported a flake-tolerated PASS; this repo runs through that script's generic
`--fallback-command` arm, which cannot parse an arbitrary command's failure report and so
treats every failure as genuine. If the Test stage reports a failure you cannot reproduce,
re-run it with `cargo test <name> -- --exact --test-threads=1` before assuming it is real.
